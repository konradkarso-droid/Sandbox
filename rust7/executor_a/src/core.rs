//! Состояние среды и правила: объекты, ворота, фиксация, узлы, сдача
//! ответа, журнал действий. Движок WebAssembly сюда не заходит — только
//! через `Mem` (память вызывающего) и `engine.rs`.

use crate::canon::{escape_into, hex};
use crate::graph;
use crate::journal::Journal;
use crate::tasks::{self, Store as TaskStore};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::io::Write;
use std::path::PathBuf;

pub const FUEL: u64 = 3_000_000;
pub const MAX_OBJ: usize = 4096;
pub const MAX_TABLE: usize = 64;
pub const MAX_INST: usize = 4;
pub const POINTS: [&str; 10] = [
    "в начале фиксации",
    "перед списанием",
    "запись списания оборвана",
    "после списания",
    "запись доставки оборвана",
    "после доставки",
    "перед работой",
    "запись работы оборвана",
    "после работы",
    "посреди восстановления",
];

pub struct Obj {
    pub data: Vec<u8>,
    pub labels: BTreeSet<String>,
    pub refs: Vec<(String, String)>,
}

#[derive(PartialEq, Clone, Copy, Debug)]
pub enum St {
    Building,
    Live,
    Dead,
}

pub struct Inst {
    pub subject: String,
    pub table: Vec<(String, String)>,
    pub instance: Option<wasmi::Instance>,
    pub st: St,
}

pub struct Data {
    pub limits: wasmi::StoreLimits,
}

pub struct Contour {
    pub budget: i64,
    pub spent: i64,
    pub labels: BTreeSet<String>,
    pub canary: [u8; 16],
    pub insts: Vec<Inst>,
    pub store: Option<wasmi::Store<Data>>,
    pub module: Option<wasmi::Module>,
    pub agent: Option<usize>,
    pub live: BTreeMap<usize, [u8; 32]>,
    /// Узлы, вычисленные впервые, — это 0 … computed − 1: впервые узлы
    /// вычисляются только по порядку.
    pub computed: usize,
    pub fixno: u64,
    pub refusals: BTreeMap<i32, u64>,
    pub accepted: BTreeSet<(u8, u8)>,
    pub wrong: BTreeSet<(u8, u8)>,
    pub env_checks: i64,
    pub debits_pos: i64,
    pub tasks: Option<TaskStore>,
}

/// Память вызывающего экземпляра.
pub trait Mem {
    fn len(&self) -> usize;
    fn read(&self, a: usize, b: usize) -> Vec<u8>;
    fn write(&mut self, a: usize, d: &[u8]);
}

pub struct Core {
    pub kind6: bool,
    pub root: PathBuf,
    pub engine: wasmi::Engine,
    pub started: bool,
    pub run_no: u32,
    pub wd: PathBuf,
    pub cfg: BTreeMap<String, Value>,
    pub secret: [u8; 32],
    pub journal: Option<Journal>,
    pub fault: Option<(String, u64)>,
    pub counts: HashMap<String, u64>,
    pub objects: BTreeMap<String, Obj>,
    pub cs: Vec<Contour>,
}

thread_local! {
    pub static CORE: RefCell<Option<Core>> = const { RefCell::new(None) };
}

/// Короткий доступ к среде. Нельзя держать во время вызова гостя: из
/// гостя вызываются ворота, которые берут среду снова.
pub fn core<R>(f: impl FnOnce(&mut Core) -> R) -> R {
    CORE.with(|c| f(c.borrow_mut().as_mut().expect("среда не создана")))
}

pub fn strip_digit(s: &str) -> &str {
    match s.chars().last() {
        Some(c) if c.is_ascii_digit() => &s[..s.len() - 1],
        _ => s,
    }
}

fn fact(parts: &[&str]) -> Value {
    Value::Array(parts.iter().map(|p| Value::String(p.to_string())).collect())
}

pub fn text_of(bytes: &[u8]) -> String {
    match std::str::from_utf8(bytes) {
        Ok(s) => s.to_string(),
        Err(_) => format!("0x{}", hex(bytes)),
    }
}

impl Core {
    pub fn geti(&self, k: &str) -> i64 {
        self.cfg.get(k).and_then(|v| v.as_i64()).unwrap_or(0)
    }
    pub fn gets(&self, k: &str) -> String {
        self.cfg.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
    }
    pub fn rule(&self) -> String {
        if self.kind6 {
            self.gets("rule")
        } else {
            "M3".to_string()
        }
    }
    pub fn n_nodes(&self) -> usize {
        graph::size(self.geti("levels") as u32)
    }
    pub fn c(&mut self, i: usize) -> &mut Contour {
        &mut self.cs[i - 1]
    }

    // ---- сбои

    /// Прошла ли точка с назначенным сбоем. Счёт ведётся по каждой точке.
    pub fn hit(&mut self, name: &str) -> bool {
        let n = self.counts.entry(name.to_string()).or_insert(0);
        *n += 1;
        matches!(&self.fault, Some((p, k)) if p == name && *k == *n)
    }

    pub fn point(&mut self, name: &str) {
        if self.hit(name) {
            std::process::exit(17);
        }
    }

    // ---- журнал

    /// Записать запись. `torn` — имя точки «запись … оборвана»: если она
    /// сработала, на диск попадает половина строки, и процесс завершается.
    pub fn log(&mut self, rec: Value, torn: Option<&str>) {
        let crash = match torn {
            Some(p) => self.hit(p),
            None => false,
        };
        let j = self.journal.as_mut().expect("журнала нет");
        let (line, hash) = j.line(&rec);
        if crash {
            j.write_bytes(&line[..line.len() / 2]);
            std::process::exit(17);
        }
        j.write_bytes(&line);
        j.commit(hash);
    }

    /// Запись «действие» (§8).
    pub fn action(&mut self, subject: &str, contour: usize, rule: &str, obj: Option<&str>, facts: Vec<Value>) {
        let (kind, rel) = match obj {
            None => ("-".to_string(), "-".to_string()),
            Some(o) => {
                let kind = strip_digit(o).to_string();
                let rel = if subject == "человек" {
                    "-"
                } else if o == "резольвер" {
                    "служба"
                } else if strip_digit(o).len() == o.len() {
                    "общий"
                } else if o.ends_with(&contour.to_string()) {
                    "свой"
                } else {
                    "чужой"
                };
                (kind, rel.to_string())
            }
        };
        self.log(
            json!({"kind": "действие", "subject": subject, "signature": [rule, kind, rel], "facts": facts}),
            None,
        );
    }

    fn label_facts(obj_or_mem: &str, labels: &BTreeSet<String>) -> Vec<Value> {
        labels.iter().map(|x| fact(&["сведения", obj_or_mem, x])).collect()
    }

    // ---- получатели

    pub fn recv_path(&self, recv: &str, i: usize) -> PathBuf {
        self.wd.join(format!("{recv}{i}.jsonl"))
    }

    fn recv_line(fix: &str, text: &str) -> String {
        let mut s = String::from("{\"fix\": ");
        escape_into(fix, &mut s);
        s.push_str(", \"msgs\": [");
        escape_into(text, &mut s);
        s.push_str("]}\n");
        s
    }

    /// Принять (№, [текст]): пара уже есть — ничего не писать.
    pub fn receive(&mut self, recv: &str, i: usize, fix: &str, text: &str) {
        let path = self.recv_path(recv, i);
        if let Ok(data) = std::fs::read(&path) {
            let end = data.iter().rposition(|b| *b == b'\n').map(|p| p + 1).unwrap_or(0);
            for line in String::from_utf8_lossy(&data[..end]).lines() {
                if let Ok(v) = serde_json::from_str::<Value>(line) {
                    if v["fix"].as_str() == Some(fix) && v["msgs"].as_array().map_or(false, |m| m.iter().any(|x| x.as_str() == Some(text))) {
                        return;
                    }
                }
            }
        }
        let line = Self::recv_line(fix, text).into_bytes();
        let torn = self.hit("запись доставки оборвана");
        let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&path).expect("получатель");
        if torn {
            let _ = f.write_all(&line[..line.len() / 2]);
            let _ = f.sync_all();
            std::process::exit(17);
        }
        let _ = f.write_all(&line);
        let _ = f.sync_all();
    }

    // ---- учёт

    pub fn price(&self, i: usize) -> i64 {
        let c = self.geti("price");
        if self.rule() == "M2" {
            c * (1 + self.cs[i - 1].debits_pos)
        } else {
            c
        }
    }

    /// Остаток для проверок: при M0 — бесконечный.
    pub fn rest(&self, i: usize) -> i64 {
        if self.rule() == "M0" {
            i64::MAX / 4
        } else {
            self.cs[i - 1].budget
        }
    }

    fn spend(&mut self, i: usize, x: i64) {
        let c = &mut self.cs[i - 1];
        c.budget -= x;
        c.spent += x;
    }

    /// Списать плату за отказ; вернуть списанное.
    fn charge_refusal(&mut self, i: usize) -> i64 {
        let ck = self.geti("fail_price");
        let ch = if self.rule() == "M0" { ck } else { ck.min(self.cs[i - 1].budget.max(0)) };
        self.spend(i, ch);
        ch
    }

    fn next_fix(&mut self, i: usize) -> String {
        let c = &mut self.cs[i - 1];
        let n = c.fixno;
        c.fixno += 1;
        format!("{i}-{n}")
    }

    // ---- ворота

    fn bounded(a: i32, b: i32, len: usize) -> bool {
        a >= 0 && b >= 0 && (a as i64 + b as i64) <= len as i64
    }

    pub fn gate(&mut self, i: usize, idx: usize, mut mem: Option<&mut dyn Mem>, num: i32, op: i32, a: i32, b: i32) -> i32 {
        let r = self.gate_inner(i, idx, &mut mem, num, op, a, b);
        if r < 0 {
            *self.cs[i - 1].refusals.entry(r).or_insert(0) += 1;
        }
        r
    }

    fn gate_inner(&mut self, i: usize, idx: usize, mem: &mut Option<&mut dyn Mem>, num: i32, op: i32, a: i32, b: i32) -> i32 {
        let subj = self.cs[i - 1].insts[idx].subject.clone();
        let entry = {
            let t = &self.cs[i - 1].insts[idx].table;
            if num >= 0 && (num as usize) < t.len() {
                Some(t[num as usize].clone())
            } else {
                None
            }
        };
        let own_out = format!("выход{i}");
        let own_e = format!("E{i}");
        let own_nodes = format!("узлы{i}");
        if let Some((obj, right)) = &entry {
            if right == "зап" && *obj == own_out {
                if self.kind6 && op == 8 {
                    return self.submit(i, &subj, mem, a, b);
                }
                if self.kind6 && op == 1 {
                    return -1;
                }
                if !self.kind6 && op == 1 {
                    return self.fix_bytes(i, &subj, mem, "выход", &own_out, a, b);
                }
                if op == 5 {
                    return self.deliver(i, &subj, "выход", &own_out, a);
                }
            }
            if right == "зап" && *obj == own_e {
                if op == 1 {
                    return self.fix_bytes(i, &subj, mem, "E", &own_e, a, b);
                }
                if op == 5 {
                    return self.deliver(i, &subj, "E", &own_e, a);
                }
            }
            if right == "выз" && *obj == own_nodes {
                return match op {
                    6 => self.compute(i, &subj, a),
                    7 => self.drop_node(i, &subj, a),
                    _ => -1,
                };
            }
        }
        let Some(m) = mem.as_deref_mut() else { return -4 };
        let Some((obj, right)) = entry else { return -1 };
        let memlen = m.len();
        match op {
            0 if right == "чт" => {
                if !Self::bounded(a, b, memlen) {
                    return -4;
                }
                let o = self.objects.get(&obj).expect("объект");
                let n = (b as usize).min(o.data.len());
                if n > 0 {
                    m.write(a as usize, &o.data[..n]);
                }
                let ol = o.labels.clone();
                self.cs[i - 1].labels.extend(ol.iter().cloned());
                let facts = Self::label_facts(&format!("память{i}"), &ol);
                self.action(&subj, i, "чтение", Some(&obj), facts);
                n as i32
            }
            1 if right == "зап" => {
                if !Self::bounded(a, b, memlen) {
                    return -4;
                }
                if self.objects[&obj].data.len() + b as usize > MAX_OBJ {
                    return -5;
                }
                let bytes = m.read(a as usize, b as usize);
                let ml = self.cs[i - 1].labels.clone();
                let o = self.objects.get_mut(&obj).unwrap();
                o.data.extend_from_slice(&bytes);
                o.labels.extend(ml.iter().cloned());
                let facts = Self::label_facts(&obj, &ml);
                self.action(&subj, i, "запись", Some(&obj), facts);
                b
            }
            2 if right == "выз" && obj == "резольвер" => {
                if !Self::bounded(a, b, memlen) {
                    return -4;
                }
                let bytes = m.read(a as usize, b as usize);
                let v = match std::str::from_utf8(&bytes) {
                    Ok("версия") => 5,
                    Ok("цена") => 1,
                    Ok("контуров") => 2,
                    _ => return -2,
                };
                self.action(&subj, i, "резольвер: имя из описи", Some("резольвер"), vec![fact(&["вызов", &subj, "резольвер"])]);
                v
            }
            3 if right == "зап" => {
                let table = self.cs[i - 1].insts[idx].table.clone();
                if a < 0 || a as usize >= table.len() {
                    return -1;
                }
                if self.objects[&obj].refs.len() >= MAX_TABLE {
                    return -5;
                }
                let (o2, p2) = table[a as usize].clone();
                self.objects.get_mut(&obj).unwrap().refs.push((o2.clone(), p2.clone()));
                self.action(&subj, i, "передача ссылки", Some(&obj), vec![fact(&["ссылка", &obj, &o2, &p2])]);
                0
            }
            4 if right == "чт" => {
                let refs = &self.objects[&obj].refs;
                if a < 0 || a as usize >= refs.len() {
                    return -1;
                }
                let (o2, p2) = refs[a as usize].clone();
                if self.cs[i - 1].insts[idx].table.len() >= MAX_TABLE {
                    return -5;
                }
                let t = &mut self.cs[i - 1].insts[idx].table;
                t.push((o2.clone(), p2.clone()));
                let new = t.len() as i32 - 1;
                self.action(&subj, i, "чтение ссылки", Some(&obj), vec![fact(&["право", &subj, &o2, &p2])]);
                new
            }
            _ => -1,
        }
    }

    // ---- фиксация

    fn fix_bytes(&mut self, i: usize, subj: &str, mem: &mut Option<&mut dyn Mem>, recv: &str, obj: &str, a: i32, b: i32) -> i32 {
        let Some(m) = mem.as_deref_mut() else { return -4 };
        if !Self::bounded(a, b, m.len()) {
            return -4;
        }
        if recv == "E" {
            return -1;
        }
        if self.objects[obj].data.len() + b as usize > MAX_OBJ {
            return -5;
        }
        let bytes = m.read(a as usize, b as usize);
        let text = text_of(&bytes);
        let labels = self.cs[i - 1].labels.clone();
        let cond = !bytes.is_empty();
        self.fixation(i, subj, recv, obj, text, bytes, cond, "пустое сообщение", "запись", labels, b)
    }

    fn deliver(&mut self, i: usize, subj: &str, recv: &str, obj: &str, j: i32) -> i32 {
        let n = self.n_nodes();
        let c = &self.cs[i - 1];
        let inmem = j >= 0 && c.live.contains_key(&(j as usize));
        let (cond, why) = if recv == "E" {
            (j >= 0 && j as usize == n - 1 && inmem && c.computed == n, "не сток, нет в памяти или уровень меньше N")
        } else {
            (j >= 0 && (j as usize) < n && inmem, "узла нет в памяти")
        };
        let text = if cond {
            format!("узел {j}: {}", hex(&c.live[&(j as usize)]))
        } else {
            format!("узел {j}")
        };
        let labels = self.objects[&format!("узлы{i}")].labels.clone();
        let bytes = text.clone().into_bytes();
        self.fixation(i, subj, recv, obj, text, bytes, cond, why, "доставка узла", labels, 0)
    }

    /// Общий протокол фиксации (§10.3).
    #[allow(clippy::too_many_arguments)]
    fn fixation(&mut self, i: usize, subj: &str, recv: &str, obj: &str, text: String, bytes: Vec<u8>, cond: bool, why: &str, rule: &str, labels: BTreeSet<String>, ok: i32) -> i32 {
        let fix = self.next_fix(i);
        self.point("в начале фиксации");
        let p = self.price(i);
        if !cond || self.rest(i) < p {
            let ch = self.charge_refusal(i);
            self.log(
                json!({"kind": "отказ", "contour": i, "subject": subj, "fix": fix, "receiver": recv, "msg": text,
                       "charge": ch, "why": if cond { "мало бюджета" } else { why }}),
                None,
            );
            return if cond { -3 } else { -6 };
        }
        self.point("перед списанием");
        self.spend(i, p);
        if p > 0 {
            self.cs[i - 1].debits_pos += 1;
        }
        self.log(
            json!({"kind": "списание", "contour": i, "subject": subj, "fix": fix, "receiver": recv, "msg": text, "price": p}),
            Some("запись списания оборвана"),
        );
        self.point("после списания");
        self.receive(recv, i, &fix, &text);
        self.point("после доставки");
        self.log(json!({"kind": "завершено", "contour": i, "fix": fix}), None);
        let o = self.objects.get_mut(obj).unwrap();
        o.data.extend_from_slice(&bytes);
        o.data.truncate(MAX_OBJ);
        o.labels.extend(labels.iter().cloned());
        let facts = Self::label_facts(obj, &labels);
        self.action(subj, i, rule, Some(obj), facts);
        ok
    }

    // ---- сдача ответа

    fn refuse_answer(&mut self, i: usize, subj: &str, fix: &str, why: &str, t: i64, p: i64, code: i32) -> i32 {
        let ch = self.charge_refusal(i);
        self.log(
            json!({"kind": "отказ", "contour": i, "subject": subj, "fix": fix, "receiver": "выход", "msg": "",
                   "charge": ch, "why": why, "task": t, "part": p}),
            None,
        );
        code
    }

    fn submit(&mut self, i: usize, subj: &str, mem: &mut Option<&mut dyn Mem>, a: i32, b: i32) -> i32 {
        let Some(m) = mem.as_deref_mut() else { return -4 };
        if !Self::bounded(a, b, m.len()) {
            return -4;
        }
        let msg = m.read(a as usize, b as usize);
        let fix = self.next_fix(i);
        self.point("в начале фиксации");
        let (t, p) = if msg.len() >= 2 { (msg[0] as i64, msg[1] as i64) } else { (-1, -1) };
        let answer: Vec<u8> = if msg.len() >= 2 { msg[2..].to_vec() } else { Vec::new() };
        let rec = if t >= 0 {
            let c = &self.cs[i - 1];
            let ts = c.tasks.as_ref().unwrap();
            ts.index.get(&(t as u8, p as u8)).map(|n| ts.parts[*n].rec.clone())
        } else {
            None
        };
        let Some(rec) = rec else {
            return self.refuse_answer(i, subj, &fix, "нет такой задачи", t, p, -6);
        };
        let key = (t as u8, p as u8);
        if self.cs[i - 1].accepted.contains(&key) {
            return self.refuse_answer(i, subj, &fix, "уже принято", t, p, -6);
        }
        let agent_pays = self.cs[i - 1].wrong.contains(&key);
        let v = tasks::check_cost(&rec, &answer, self.geti("check_unit"));
        let price = self.price(i);
        if self.rest(i) < price + if agent_pays { v } else { 0 } {
            return self.refuse_answer(i, subj, &fix, "мало бюджета", t, p, -3);
        }
        self.log(
            json!({"kind": "проверка", "contour": i, "subject": subj, "fix": fix, "task": t, "part": p,
                   "payer": if agent_pays { "агент" } else { "среда" }, "price": v, "length": answer.len()}),
            None,
        );
        if agent_pays {
            self.spend(i, v);
        } else {
            self.cs[i - 1].env_checks += v;
        }
        if !tasks::is_correct(&rec, &answer, self.geti("canonical") != 0) {
            self.cs[i - 1].wrong.insert(key);
            return self.refuse_answer(i, subj, &fix, "ответ неверен", t, p, -6);
        }
        self.point("перед списанием");
        self.spend(i, price);
        if price > 0 {
            self.cs[i - 1].debits_pos += 1;
        }
        let text = format!("задача {t}.{p}: {}", hex(&answer));
        self.log(
            json!({"kind": "списание", "contour": i, "subject": subj, "fix": fix, "receiver": "выход", "msg": text,
                   "price": price, "task": t, "part": p}),
            Some("запись списания оборвана"),
        );
        self.point("после списания");
        self.receive("выход", i, &fix, &text);
        self.point("после доставки");
        self.log(json!({"kind": "завершено", "contour": i, "fix": fix}), None);
        self.cs[i - 1].accepted.insert(key);
        let labels = self.cs[i - 1].labels.clone();
        let out = format!("выход{i}");
        let o = self.objects.get_mut(&out).unwrap();
        o.data.extend_from_slice(&answer);
        o.data.truncate(MAX_OBJ);
        o.labels.extend(labels.iter().cloned());
        let facts = Self::label_facts(&out, &labels);
        self.action(subj, i, "сдача ответа", Some(&out), facts);
        0
    }

    // ---- узлы

    fn compute(&mut self, i: usize, subj: &str, j: i32) -> i32 {
        let n = self.n_nodes();
        let memory = self.geti("memory");
        let (first, preds) = {
            let c = &self.cs[i - 1];
            if j < 0 || j as usize >= n || c.live.contains_key(&(j as usize)) {
                return -6;
            }
            let j = j as usize;
            if c.live.len() as i64 >= memory {
                return -5;
            }
            let first = j >= c.computed;
            if first && j != c.computed {
                return -6;
            }
            let preds = graph::preds(&self.gets("graph"), j);
            if preds.iter().any(|p| !c.live.contains_key(p)) {
                return -6;
            }
            (first, preds)
        };
        let j = j as usize;
        let wp = self.geti("work_price");
        if self.rest(i) < wp {
            return -3;
        }
        self.point("перед работой");
        self.spend(i, wp);
        self.log(
            json!({"kind": "работа", "contour": i, "subject": subj, "node": j, "first": first, "preds": preds, "price": wp}),
            Some("запись работы оборвана"),
        );
        self.point("после работы");
        let pv: Vec<[u8; 32]> = preds.iter().map(|p| self.cs[i - 1].live[p]).collect();
        let v = graph::node_value(&self.secret, i, j, &pv);
        let c = &mut self.cs[i - 1];
        c.live.insert(j, v);
        if first {
            c.computed += 1;
        }
        let ml = c.labels.clone();
        let nodes = format!("узлы{i}");
        self.objects.get_mut(&nodes).unwrap().labels.extend(ml.iter().cloned());
        let facts = Self::label_facts(&nodes, &ml);
        self.action(subj, i, "вычисление узла", Some(&nodes), facts);
        0
    }

    fn drop_node(&mut self, i: usize, subj: &str, j: i32) -> i32 {
        if j < 0 || self.cs[i - 1].live.remove(&(j as usize)).is_none() {
            return -6;
        }
        self.log(json!({"kind": "выброс", "contour": i, "subject": subj, "node": j, "price": 0}), None);
        let nodes = format!("узлы{i}");
        self.action(subj, i, "выброс узла", Some(&nodes), vec![fact(&["вызов", subj, &nodes])]);
        0
    }

    // ---- слова человека

    pub fn say(&mut self, i: usize, text: &str) {
        let name = format!("вход{i}");
        let o = self.objects.get_mut(&name).unwrap();
        let room = MAX_OBJ.saturating_sub(o.data.len());
        let b = text.as_bytes();
        o.data.extend_from_slice(&b[..b.len().min(room)]);
        o.labels.insert("Ч".to_string());
        self.action("человек", i, "слова человека", Some(&name), vec![fact(&["сведения", &name, "Ч"])]);
    }
}
