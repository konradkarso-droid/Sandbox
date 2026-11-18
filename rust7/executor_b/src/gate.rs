//! Ворота `call`, фиксация, узлы, сдача ответа (контракт, §10, §11.5).
//!
//! Код работает с памятью вызывающего через `Mem`, а не через движок:
//! так логика ворот не зависит от wasmi и проверяется обычными тестами.

use crate::fault;
use crate::graph;
use crate::json::hex;
use crate::tasks;
use crate::world::{OBJECT_BYTES, TABLE_ENTRIES, World, rec};
use serde_json::{Value, json};
use std::collections::BTreeSet;

/// Почему попытка оборвана на шаге 0.
pub enum Halt {
    /// Контур остановлен: ловушка «бюджет исчерпан».
    Stop,
    /// Нечем платить в стартовой функции создаваемого помощника с этим номером.
    AbortSpawn(usize),
}

/// Экспортированная память вызывающего экземпляра.
pub trait Mem {
    /// Длина в байтах; `None` — памяти `memory` нет.
    fn len(&self) -> Option<usize>;
    fn read(&self, at: usize, n: usize) -> Vec<u8>;
    fn write(&mut self, at: usize, data: &[u8]);
}

fn facts_for(obj: &str, labels: &BTreeSet<String>) -> Vec<Value> {
    labels.iter().map(|x| json!(["сведения", obj, x])).collect()
}

/// Текст сообщения: UTF-8, если байты — правильный UTF-8, иначе `0x` и шестнадцатеричные.
fn message_text(b: &[u8]) -> String {
    match std::str::from_utf8(b) {
        Ok(s) => s.to_string(),
        Err(_) => format!("0x{}", hex(b)),
    }
}

/// Всё, что нужно, чтобы довести фиксацию от списания до эффекта.
struct Commit<'a> {
    ci: usize,
    subject: &'a str,
    recv_name: String,
    recv_kind: &'a str,
    text: String,
    bytes: Vec<u8>,
    rule: &'a str,
    labels: BTreeSet<String>,
    ok_ret: i32,
    task: Option<(i64, i64)>,
    price: i64,
}

impl World {
    /// Вход ворот. Каждый отрицательный возврат засчитывается в отказы
    /// контура по коду.
    /// `Err` — шаг 0 попытки не пропустил её: вызывающий обязан оборвать вызов
    /// ловушкой (см. `Halt`).
    pub fn gate_call(&mut self, ci: usize, idx: usize, mem: &mut dyn Mem, num: i32, op: i32, a: i32, b: i32) -> Result<i32, Halt> {
        if let Some(h) = self.attempt_blocked(ci, idx) {
            return Err(h);
        }
        self.own_refusal = false;
        let r = self.gate(ci, idx, mem, num, op, a, b);
        if r < 0 {
            *self.contours[ci].refusals.entry(r as i64).or_insert(0) += 1;
            if self.cfg.paid && !self.own_refusal {
                let subject = self.contours[ci].insts[idx].subject.clone();
                self.gate_refusal(ci, &subject, Some((num, op)), r);
            }
        }
        Ok(r)
    }

    /// Шаг 0 попытки (`call` и `spawn`): `Some` — попытка не выполняется.
    /// Остановленный контур молчит; при остатке ≤ 0 пишется единственный
    /// «останов». Исключение — экземпляр, чья стартовая функция ещё идёт при
    /// создании помощника: остаток мог опустеть только из-за платы 2g за него,
    /// поэтому обрывается создание, а не контур. Без правила «попытка
    /// платит» не делает ничего.
    pub fn attempt_blocked(&mut self, ci: usize, idx: usize) -> Option<Halt> {
        if !self.cfg.paid {
            return None;
        }
        if self.contours[ci].stopped {
            return Some(Halt::Stop);
        }
        if self.remaining(ci) > 0 {
            return None;
        }
        if self.contours[ci].starting.contains(&idx) {
            return Some(Halt::AbortSpawn(idx));
        }
        let subject = self.contours[ci].insts[idx].subject.clone();
        self.contours[ci].stopped = true;
        self.emit(rec(json!({"kind": "останов", "contour": ci + 1, "subject": subject, "why": "бюджет исчерпан"})), None);
        Some(Halt::Stop)
    }

    /// Плата за отказ, который не записал свой «отказ»: min(g, остаток), при
    /// M0 — g. Шаг 0 гарантирует остаток больше 0, поэтому плата не меньше 1.
    /// `call_args` — номер и операция вызова `call`; для `spawn` их нет.
    pub fn gate_refusal(&mut self, ci: usize, subject: &str, call_args: Option<(i32, i32)>, code: i32) {
        let g = self.cfg.gate_price;
        let charge = if self.cfg.rule == "M0" { g } else { g.min(self.remaining(ci).max(0)) };
        let mut r = rec(json!({"kind": "отказ ворот", "contour": ci + 1, "subject": subject}));
        match call_args {
            Some((cap, op)) => {
                r.insert("call".into(), json!("call"));
                r.insert("cap".into(), json!(cap));
                r.insert("op".into(), json!(op));
            }
            None => {
                r.insert("call".into(), json!("spawn"));
            }
        }
        r.insert("code".into(), json!(code));
        r.insert("charge".into(), json!(charge));
        self.emit(r, None);
    }

    fn gate(&mut self, ci: usize, idx: usize, mem: &mut dyn Mem, num: i32, op: i32, a: i32, b: i32) -> i32 {
        let i = ci + 1;
        let subject = self.contours[ci].insts[idx].subject.clone();
        let entry = if num >= 0 { self.contours[ci].insts[idx].table.get(num as usize).cloned() } else { None };
        let memlen = mem.len();
        let g = self.cfg.gate_price;
        let in_bounds = matches!(memlen, Some(l) if a >= 0 && b >= 0 && a as i64 + b as i64 <= l as i64);

        // Особые случаи — только для объектов своего контура.
        if let Some((obj, right)) = &entry {
            let is_out = *obj == format!("выход{i}");
            let is_e = *obj == format!("E{i}");
            if right == "зап" && (is_out || is_e) {
                if op == 8 && self.cfg.kind6 && is_out {
                    return self.submit(ci, &subject, mem, in_bounds, a, b);
                }
                if op == 1 && self.cfg.kind6 && is_out {
                    return -1;
                }
                if op == 1 {
                    return self.fix_bytes(ci, &subject, mem, in_bounds, is_out, a, b);
                }
                if op == 5 {
                    return self.deliver(ci, &subject, is_out, a as i64);
                }
            }
            if right == "выз" && *obj == format!("модель{i}") {
                return if op == 2 { self.ask_model(ci, &subject, mem, in_bounds, a, b) } else { -1 };
            }
            if right == "выз" && *obj == format!("узлы{i}") {
                return match op {
                    6 => self.compute(ci, &subject, a as i64),
                    7 => self.drop_node(ci, &subject, a as i64),
                    _ => -1,
                };
            }
        }
        if memlen.is_none() {
            return -4;
        }
        let Some((obj, right)) = entry else { return -1 };
        let mem_labels = self.contours[ci].labels.clone();
        match (op, right.as_str()) {
            (0, "чт") => {
                if !in_bounds {
                    return -4;
                }
                if self.remaining(ci) < g {
                    return -3;
                }
                let o = &self.objects[&obj];
                let n = (b as usize).min(o.data.len());
                if n > 0 {
                    mem.write(a as usize, &o.data[..n]);
                }
                let labels = o.labels.clone();
                self.contours[ci].labels.extend(labels.iter().cloned());
                self.action_charged(&subject, "чтение", Some(&obj), facts_for(&format!("память{i}"), &labels), g);
                n as i32
            }
            (1, "зап") => {
                if !in_bounds {
                    return -4;
                }
                if self.objects[&obj].data.len() + b as usize > OBJECT_BYTES {
                    return -5;
                }
                if self.remaining(ci) < g {
                    return -3;
                }
                let bytes = mem.read(a as usize, b as usize);
                let o = self.objects.get_mut(&obj).unwrap();
                o.data.extend_from_slice(&bytes);
                o.labels.extend(mem_labels.iter().cloned());
                self.action_charged(&subject, "запись", Some(&obj), facts_for(&obj, &mem_labels), g);
                b
            }
            (2, "выз") if obj == "резольвер" => {
                if !in_bounds {
                    return -4;
                }
                let bytes = mem.read(a as usize, b as usize);
                let v = match std::str::from_utf8(&bytes) {
                    Ok("версия") => 5,
                    Ok("цена") => 1,
                    Ok("контуров") => 2,
                    Ok("модель") if self.cfg.model => self.cfg.model_mode as i32,
                    _ => return -2,
                };
                if self.remaining(ci) < g {
                    return -3;
                }
                self.action_charged(&subject, "резольвер: имя из описи", Some("резольвер"), vec![json!(["вызов", subject, "резольвер"])], g);
                v
            }
            (3, "зап") => {
                let t = &self.contours[ci].insts[idx].table;
                if a < 0 || a as usize >= t.len() {
                    return -1;
                }
                let pair = t[a as usize].clone();
                let o = self.objects.get_mut(&obj).unwrap();
                if o.refs.len() >= TABLE_ENTRIES {
                    return -5;
                }
                if self.cfg.rule != "M0" && self.cfg.budget - self.contours[ci].spent < g {
                    return -3;
                }
                let o = self.objects.get_mut(&obj).unwrap();
                o.refs.push(pair.clone());
                self.action_charged(&subject, "передача ссылки", Some(&obj), vec![json!(["ссылка", obj, pair.0, pair.1])], g);
                0
            }
            (4, "чт") => {
                let o = &self.objects[&obj];
                if a < 0 || a as usize >= o.refs.len() {
                    return -1;
                }
                let pair = o.refs[a as usize].clone();
                let t = &mut self.contours[ci].insts[idx].table;
                if t.len() >= TABLE_ENTRIES {
                    return -5;
                }
                if self.remaining(ci) < g {
                    return -3;
                }
                let t = &mut self.contours[ci].insts[idx].table;
                t.push(pair.clone());
                let new = (t.len() - 1) as i32;
                self.action_charged(&subject, "чтение ссылки", Some(&obj), vec![json!(["право", subject, pair.0, pair.1])], g);
                new
            }
            _ => -1,
        }
    }

    // ---- цена и остаток

    pub fn price_p(&self, ci: usize) -> i64 {
        if self.cfg.rule == "M2" {
            self.cfg.price * (1 + self.contours[ci].paid_debits)
        } else {
            self.cfg.price
        }
    }

    /// Остаток для проверок: при M0 считается бесконечным.
    fn remaining(&self, ci: usize) -> i64 {
        if self.cfg.rule == "M0" {
            i64::MAX / 4
        } else {
            self.cfg.budget - self.contours[ci].spent
        }
    }

    // ---- фиксация (§10.3)

    fn alloc_fix(&mut self, ci: usize) -> String {
        let c = &mut self.contours[ci];
        let n = c.fixno;
        c.fixno += 1;
        format!("{}-{n}", ci + 1)
    }

    fn refuse(&mut self, ci: usize, subject: &str, fix: &str, recv_kind: &str, msg: &str, why: &str, task: Option<(i64, i64)>) {
        self.own_refusal = true;
        let rem = self.cfg.budget - self.contours[ci].spent;
        let charge = if self.cfg.rule == "M0" { self.cfg.fail_price } else { self.cfg.fail_price.min(rem.max(0)) };
        let mut r = rec(json!({"kind": "отказ", "contour": ci + 1, "subject": subject, "fix": fix,
            "receiver": recv_kind, "msg": msg, "charge": charge, "why": why}));
        if let Some((t, p)) = task {
            r.insert("task".into(), json!(t));
            r.insert("part".into(), json!(p));
        }
        self.emit(r, None);
    }

    /// Шаги 4–11 фиксации: от «перед списанием» до эффекта.
    fn commit(&mut self, fix: &str, c: Commit) -> i32 {
        fault::point("перед списанием");
        let mut r = rec(json!({"kind": "списание", "contour": c.ci + 1, "subject": c.subject, "fix": fix,
            "receiver": c.recv_kind, "msg": c.text, "price": c.price}));
        if let Some((t, p)) = c.task {
            r.insert("task".into(), json!(t));
            r.insert("part".into(), json!(p));
        }
        self.emit(r, Some("запись списания оборвана"));
        fault::point("после списания");
        self.recv.accept(&format!("{}{}", c.recv_kind, c.ci + 1), fix, &c.text);
        fault::point("после доставки");
        self.emit(rec(json!({"kind": "завершено", "contour": c.ci + 1, "fix": fix})), None);
        let o = self.objects.get_mut(&c.recv_name).unwrap();
        let room = OBJECT_BYTES.saturating_sub(o.data.len());
        o.data.extend_from_slice(&c.bytes[..c.bytes.len().min(room)]);
        o.labels.extend(c.labels.iter().cloned());
        let facts = facts_for(&c.recv_name, &c.labels);
        self.action(c.subject, c.rule, Some(&c.recv_name), facts);
        c.ok_ret
    }

    /// Общая часть фиксации байтов и доставки узла: номер, условие, цена.
    #[allow(clippy::too_many_arguments)]
    fn fixation(
        &mut self,
        ci: usize,
        subject: &str,
        is_out: bool,
        cond: bool,
        why: &str,
        text: String,
        bytes: Vec<u8>,
        rule: &str,
        labels: BTreeSet<String>,
        ok_ret: i32,
    ) -> i32 {
        let i = ci + 1;
        let (recv_kind, recv_name) = if is_out { ("выход", format!("выход{i}")) } else { ("E", format!("E{i}")) };
        let fix = self.alloc_fix(ci);
        fault::point("в начале фиксации");
        let price = self.price_p(ci);
        if !cond || self.remaining(ci) < price {
            self.refuse(ci, subject, &fix, recv_kind, &text, if cond { "мало бюджета" } else { why }, None);
            return if cond { -3 } else { -6 };
        }
        self.commit(&fix, Commit { ci, subject, recv_name, recv_kind, text, bytes, rule, labels, ok_ret, task: None, price })
    }

    /// Фиксация байтов (§10.2).
    fn fix_bytes(&mut self, ci: usize, subject: &str, mem: &mut dyn Mem, in_bounds: bool, is_out: bool, a: i32, b: i32) -> i32 {
        if mem.len().is_none() || !in_bounds {
            return -4;
        }
        if !is_out {
            return -1;
        }
        let name = format!("выход{}", ci + 1);
        if self.objects[&name].data.len() + b as usize > OBJECT_BYTES {
            return -5;
        }
        let bytes = mem.read(a as usize, b as usize);
        let text = message_text(&bytes);
        let labels = self.contours[ci].labels.clone();
        let cond = !bytes.is_empty();
        self.fixation(ci, subject, true, cond, "пустое сообщение", text, bytes, "запись", labels, b)
    }

    /// Доставка узла `j` в выход или в E (§10.3).
    fn deliver(&mut self, ci: usize, subject: &str, is_out: bool, j: i64) -> i32 {
        let n = self.cfg.nodes();
        let have = self.contours[ci].slots.contains_key(&j);
        let (cond, why) = if is_out {
            (0 <= j && j < n && have, "узла нет в памяти")
        } else {
            (j == n - 1 && have && self.contours[ci].computed.len() as i64 == n, "не сток, нет в памяти или уровень меньше N")
        };
        let text = if cond { format!("узел {j}: {}", self.node_hex(ci, j)) } else { format!("узел {j}") };
        let labels = self.objects[&format!("узлы{}", ci + 1)].labels.clone();
        let bytes = text.clone().into_bytes();
        self.fixation(ci, subject, is_out, cond, why, text, bytes, "доставка узла", labels, 0)
    }

    // ---- узлы (§10.4)

    fn compute(&mut self, ci: usize, subject: &str, j: i64) -> i32 {
        let n = self.cfg.nodes();
        let i = ci + 1;
        if j < 0 || j >= n {
            return -6;
        }
        let c = &self.contours[ci];
        if c.slots.contains_key(&j) {
            return -6;
        }
        if c.slots.len() as i64 >= self.cfg.memory {
            return -5;
        }
        let first = !c.computed.contains(&j);
        if first && j != c.computed.len() as i64 {
            return -6;
        }
        let preds = graph::preds(&self.cfg.graph, j);
        if preds.iter().any(|p| !c.slots.contains_key(p)) {
            return -6;
        }
        if self.remaining(ci) < self.cfg.work_price {
            return -3;
        }
        fault::point("перед работой");
        let r = rec(json!({"kind": "работа", "contour": i, "subject": subject, "node": j, "first": first,
            "preds": preds, "price": self.cfg.work_price}));
        self.emit(r, Some("запись работы оборвана"));
        fault::point("после работы");
        let pv: Vec<[u8; 32]> = preds.iter().map(|p| self.contours[ci].slots[p]).collect();
        let v = graph::value(&self.secret, i as i64, j, &pv);
        self.contours[ci].slots.insert(j, v);
        let labels = self.contours[ci].labels.clone();
        let name = format!("узлы{i}");
        self.objects.get_mut(&name).unwrap().labels.extend(labels.iter().cloned());
        self.action(subject, "вычисление узла", Some(&name), facts_for(&name, &labels));
        0
    }

    fn drop_node(&mut self, ci: usize, subject: &str, j: i64) -> i32 {
        if !self.contours[ci].slots.contains_key(&j) {
            return -6;
        }
        let price = 2 * self.cfg.gate_price;
        if self.remaining(ci) < price {
            return -3;
        }
        self.contours[ci].slots.remove(&j);
        self.emit(rec(json!({"kind": "выброс", "contour": ci + 1, "subject": subject, "node": j, "price": price})), None);
        let name = format!("узлы{}", ci + 1);
        self.action(subject, "выброс узла", Some(&name), vec![json!(["вызов", subject, name])]);
        0
    }

    // ---- сдача ответа (§11.5)

    fn submit(&mut self, ci: usize, subject: &str, mem: &mut dyn Mem, in_bounds: bool, a: i32, b: i32) -> i32 {
        if mem.len().is_none() || !in_bounds {
            return -4;
        }
        let msg = mem.read(a as usize, b as usize);
        let fix = self.alloc_fix(ci);
        fault::point("в начале фиксации");
        let (t, p) = if msg.len() >= 2 { (msg[0] as i64, msg[1] as i64) } else { (-1, -1) };
        let answer = if msg.len() >= 2 { &msg[2..] } else { &msg[..] };
        let tp = Some((t, p));
        let canonical = self.cfg.canonical;
        let Some((correct, w)) = self.tasks[ci].parts.get(&(t, p)).map(|part| tasks::check(part, answer, canonical)) else {
            self.refuse(ci, subject, &fix, "выход", "", "нет такой задачи", tp);
            return -6;
        };
        if self.contours[ci].accepted.contains(&(t, p)) {
            self.refuse(ci, subject, &fix, "выход", "", "уже принято", tp);
            return -6;
        }
        let agent_pays = self.contours[ci].wrong.contains(&(t, p));
        let v = ((w + self.cfg.check_unit - 1) / self.cfg.check_unit).max(1) * self.cfg.check_price;
        let price = self.price_p(ci);
        if self.remaining(ci) < price + if agent_pays { v } else { 0 } {
            self.refuse(ci, subject, &fix, "выход", "", "мало бюджета", tp);
            return -3;
        }
        let payer = if agent_pays { "агент" } else { "среда" };
        self.emit(
            rec(json!({"kind": "проверка", "contour": ci + 1, "subject": subject, "fix": fix, "task": t,
                "part": p, "payer": payer, "price": v, "length": answer.len()})),
            None,
        );
        if !correct {
            self.refuse(ci, subject, &fix, "выход", "", "ответ неверен", tp);
            return -6;
        }
        let text = format!("задача {t}.{p}: {}", hex(answer));
        let labels = self.contours[ci].labels.clone();
        let bytes = answer.to_vec();
        self.commit(
            &fix,
            Commit { ci, subject, recv_name: format!("выход{}", ci + 1), recv_kind: "выход", text, bytes, rule: "сдача ответа", labels, ok_ret: 0, task: tp, price },
        )
    }

    // ---- вопрос модели (§17)

    /// Ход «спросить модель»: шаги 1–11 контракта. До шага 7 отказы не пишут
    /// своей записи (их оплачивает «отказ ворот», если включено правило
    /// «попытка платит»); после него вопрос уже ушёл, и отказ −7 записан
    /// самим ответом.
    fn ask_model(&mut self, ci: usize, subject: &str, mem: &mut dyn Mem, in_bounds: bool, a: i32, b: i32) -> i32 {
        let Some(memlen) = mem.len() else { return -4 };
        if !in_bounds {
            return -4;
        }
        if b == 0 {
            return -6;
        }
        if b > 4096 {
            return -5;
        }
        let max = self.cfg.model_answer;
        if a as i64 + max > memlen as i64 {
            return -4;
        }
        if !crate::model::alive(ci) {
            return -7;
        }
        // Длина ответа до отправки неизвестна, поэтому за него берётся предел.
        let unit = self.cfg.model_byte_unit;
        let byte_fee = if unit > 0 { (b as i64 + max + unit - 1) / unit } else { 0 };
        let price = 2 * self.cfg.gate_price + byte_fee;
        if self.remaining(ci) < price {
            return -3;
        }
        let name = format!("модель{}", ci + 1);
        let question = mem.read(a as usize, b as usize);
        let labels = self.contours[ci].labels.clone();
        self.action_with(
            subject,
            "вопрос модели",
            Some(&name),
            facts_for(&name, &labels),
            price,
            vec![("question", json!(message_text(&question))), ("length", json!(b))],
        );
        self.objects.get_mut(&name).unwrap().labels.extend(labels);
        let Some(mut answer) = crate::model::ask(ci, &question, max as usize) else {
            self.own_refusal = true;
            self.action_with(subject, "ответ модели", Some(&name), vec![], 0, vec![("answer", Value::Null), ("code", json!(-7))]);
            return -7;
        };
        answer.truncate(max as usize);
        if !answer.is_empty() {
            mem.write(a as usize, &answer);
        }
        self.contours[ci].labels.insert("М".into());
        let facts = vec![json!(["сведения", format!("память{}", ci + 1), "М"])];
        self.action_with(
            subject,
            "ответ модели",
            Some(&name),
            facts,
            0,
            vec![("answer", json!(message_text(&answer))), ("length", json!(answer.len()))],
        );
        answer.len() as i32
    }

    // ---- помощники (§7)

    /// Отказ `spawn` с кодом: счётчик отказов и, с правилом, плата «отказом ворот».
    pub fn refuse_spawn(&mut self, ci: usize, idx_x: usize, code: i32) -> i32 {
        *self.contours[ci].refusals.entry(code as i64).or_insert(0) += 1;
        if self.cfg.paid {
            let subject = self.contours[ci].insts[idx_x].subject.clone();
            self.gate_refusal(ci, &subject, None, code);
        }
        code
    }

    /// Начало spawn: предел, место в таблице экземпляров, копия таблицы.
    pub fn spawn_begin(&mut self, ci: usize, idx_x: usize) -> Result<usize, i32> {
        if self.contours[ci].insts.len() >= crate::world::MAX_INSTANCES {
            return Err(self.refuse_spawn(ci, idx_x, -5));
        }
        // Плата 2g списывается до создания, чтобы стартовая функция помощника
        // уже тратила уменьшенный бюджет; при неудаче создания её вернёт spawn_abort.
        let price = 2 * self.cfg.gate_price;
        if self.remaining(ci) < price {
            return Err(self.refuse_spawn(ci, idx_x, -3));
        }
        self.contours[ci].spent += price;
        let table = self.contours[ci].insts[idx_x].table.clone();
        self.contours[ci].insts.push(crate::world::Inst { subject: format!("п{}", ci + 1), table });
        let k = self.contours[ci].insts.len() - 1;
        self.contours[ci].starting.push(k);
        Ok(k)
    }

    pub fn spawn_abort(&mut self, ci: usize, new_idx: usize) {
        self.contours[ci].starting.retain(|&k| k != new_idx);
        self.contours[ci].insts.truncate(new_idx);
        self.contours[ci].spent -= 2 * self.cfg.gate_price;
    }

    pub fn spawn_finish(&mut self, ci: usize, idx_x: usize, new_idx: usize) {
        self.contours[ci].starting.retain(|&k| k != new_idx);
        // Субъект обеих записей — новый помощник, а не породивший его экземпляр (§14).
        let helper = format!("п{}", ci + 1);
        let subject = helper.clone();
        // Плата списана в spawn_begin; запись «действие» сама добавляет её в
        // расход, поэтому предварительное списание здесь снимается.
        self.contours[ci].spent -= 2 * self.cfg.gate_price;
        self.action_charged(&subject, "создание помощника", None, vec![json!(["есть", helper])], 2 * self.cfg.gate_price);
        let facts: Vec<Value> = self.contours[ci].insts[idx_x]
            .table
            .iter()
            .map(|(o, r)| json!(["право", helper, o, r]))
            .collect();
        self.action(&subject, "копия прав помощнику", None, facts);
    }
}
