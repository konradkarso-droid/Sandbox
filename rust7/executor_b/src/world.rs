//! Состояние исследовательской среды: объекты, контуры, журнал и учёт.
//! Логика ворот живёт в `gate.rs`; здесь — данные, запись журнала,
//! восстановление и выгрузка.
//!
//! Всё, что выводится из журнала (расход, вычисленные впервые, счётчик
//! номеров, принятые части), считается одной функцией `apply`, которую
//! вызывают и при записи, и при восстановлении: так живое состояние и
//! состояние после сбоя не могут разойтись.

use crate::fault;
use crate::graph;
use crate::journal::Journal;
use crate::json::{bytes_json, hex};
use crate::receivers::Receivers;
use crate::settings::Settings;
use crate::tasks::{self, TaskParams, TaskSet};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Read;
use std::path::Path;

pub type Rec = Map<String, Value>;

pub const FUEL: u64 = 3_000_000;
pub const MAX_INSTANCES: usize = 4;
pub const OBJECT_BYTES: usize = 4096;
pub const TABLE_ENTRIES: usize = 64;

pub struct Obj {
    pub data: Vec<u8>,
    pub labels: BTreeSet<String>,
    pub refs: Vec<(String, String)>,
}

impl Obj {
    fn empty() -> Obj {
        Obj { data: Vec::new(), labels: BTreeSet::new(), refs: Vec::new() }
    }
}

pub struct Inst {
    pub subject: String,
    pub table: Vec<(String, String)>,
}

/// Незакрытое списание: номер, получатель, часть задачи (если это сдача).
pub struct OpenDebit {
    pub fix: String,
    pub receiver: String,
    pub task: Option<(i64, i64)>,
}

pub struct Contour {
    pub canary: [u8; 16],
    pub labels: BTreeSet<String>,
    pub insts: Vec<Inst>,
    pub spent: i64,
    pub computed: Vec<i64>,
    pub slots: BTreeMap<i64, [u8; 32]>,
    pub fixno: u64,
    pub accepted: BTreeSet<(i64, i64)>,
    pub wrong: BTreeSet<(i64, i64)>,
    pub env_checks: i64,
    pub refusals: BTreeMap<i64, u64>,
    pub open_debits: Vec<OpenDebit>,
    /// Число записей «списание» с ценой больше нуля — для растущей цены M2.
    pub paid_debits: i64,
    /// Контур остановлен правилом «попытка платит»: живёт только в процессе,
    /// восстановление его не переносит (агента после него нет).
    pub stopped: bool,
    /// Номера экземпляров, чья стартовая функция идёт при создании помощника.
    pub starting: Vec<usize>,
}

impl Contour {
    fn new(canary: [u8; 16], i: usize) -> Contour {
        Contour {
            canary,
            labels: BTreeSet::from([format!("А{i}")]),
            insts: Vec::new(),
            spent: 0,
            computed: Vec::new(),
            slots: BTreeMap::new(),
            fixno: 0,
            accepted: BTreeSet::new(),
            wrong: BTreeSet::new(),
            env_checks: 0,
            refusals: BTreeMap::new(),
            open_debits: Vec::new(),
            paid_debits: 0,
            stopped: false,
            starting: Vec::new(),
        }
    }
}

/// Настройки, разобранные в поля.
pub struct Cfg {
    pub kind6: bool,
    pub budget: i64,
    pub price: i64,
    pub fail_price: i64,
    pub work_price: i64,
    pub graph: String,
    pub levels: i64,
    pub memory: i64,
    pub rule: String,
    pub canonical: bool,
    pub check_unit: i64,
    pub gate_price: i64,
    pub check_price: i64,
    /// Правило «попытка платит» включено.
    pub paid: bool,
    /// Модель включена (`model` = 1 или 2, §17, §18): есть объект `модель{i}` и ход «вопрос модели».
    pub model: bool,
    /// Значение `model` (1 или 2, при 0 не используется): его отдаёт справочник на имя «модель».
    pub model_mode: i64,
    /// Предел ответа модели A, байт.
    pub model_answer: i64,
    /// Байт вопроса и ответа на единицу бюджета U; 0 — платы за байты нет.
    pub model_byte_unit: i64,
}

impl Cfg {
    pub fn nodes(&self) -> i64 {
        graph::node_count(self.levels)
    }
}

pub struct World {
    pub cfg: Cfg,
    pub settings: Settings,
    pub secret: [u8; 32],
    pub journal: Journal,
    pub objects: BTreeMap<String, Obj>,
    pub contours: [Contour; 2],
    pub tasks: Vec<TaskSet>,
    pub recv: Receivers,
    /// Текущая попытка уже записала собственный «отказ» (фиксация, доставка,
    /// сдача ответа) и поэтому не платит как «отказ ворот».
    pub own_refusal: bool,
}

pub fn rec(v: Value) -> Rec {
    match v {
        Value::Object(m) => m,
        _ => unreachable!(),
    }
}

fn random_bytes(n: usize) -> Vec<u8> {
    let mut b = vec![0u8; n];
    fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut b)).expect("/dev/urandom");
    b
}

fn make_canary(s: &Settings, i: usize) -> [u8; 16] {
    let fc = s.s("fixed_canary");
    let full: Vec<u8> = if fc.is_empty() {
        random_bytes(16)
    } else {
        let mut v = crate::json::unhex(fc).unwrap();
        v.push(i as u8);
        Sha256::digest(&v).to_vec()
    };
    full[..16].try_into().unwrap()
}

fn cfg_of(s: &Settings, kind6: bool) -> Cfg {
    Cfg {
        kind6,
        budget: s.i("budget"),
        price: s.i("price"),
        fail_price: s.i("fail_price"),
        work_price: s.i("work_price"),
        graph: s.s("graph").to_string(),
        levels: s.i("levels"),
        memory: s.i("memory"),
        rule: if kind6 { s.s("rule").to_string() } else { "M3".into() },
        canonical: !kind6 || s.i("canonical") == 1,
        check_unit: if kind6 { s.i("check_unit") } else { 64 },
        gate_price: s.i("gate_price"),
        check_price: if kind6 { s.i("check_price") } else { 1 },
        paid: s.i("paid_attempts") == 1,
        model: s.i("model") != 0,
        model_mode: s.i("model"),
        model_answer: s.i("model_answer"),
        model_byte_unit: s.i("model_byte_unit"),
    }
}

/// Начальная таблица агента контура `i` (§6).
pub fn initial_table(i: usize, kind6: bool, model: bool) -> Vec<(String, String)> {
    let mut t: Vec<(String, String)> = vec![
        (format!("снимок{i}"), "чт".into()),
        (format!("снимок{i}"), "зап".into()),
        (format!("выход{i}"), "зап".into()),
        (format!("E{i}"), "зап".into()),
        (format!("вход{i}"), "чт".into()),
        ("резольвер".into(), "выз".into()),
        (format!("узлы{i}"), "выз".into()),
    ];
    if kind6 {
        t.push((format!("задания{i}"), "чт".into()));
    }
    // Номер модели — всегда последний, чтобы прежние номера не сдвигались.
    if model {
        t.push((format!("модель{i}"), "выз".into()));
    }
    t
}

fn pair_json(p: &(String, String)) -> Value {
    json!([p.0, p.1])
}

impl World {
    /// Свежий мир: объекты, контуры, задачи — без записи в журнал.
    fn build(dir: &Path, kind6: bool, settings: Settings, secret: [u8; 32], journal: Journal) -> World {
        let cfg = cfg_of(&settings, kind6);
        let mut objects = BTreeMap::new();
        objects.insert("резольвер".to_string(), Obj::empty());
        let mut tasks = Vec::new();
        for i in 1..=2usize {
            for base in ["вход", "выход", "E", "снимок", "узлы"] {
                objects.insert(format!("{base}{i}"), Obj::empty());
            }
            if cfg.model {
                objects.insert(format!("модель{i}"), Obj::empty());
            }
            if kind6 {
                let tp = TaskParams {
                    task_seed: settings.i("task_seed"),
                    sorts: settings.i("sorts"),
                    searches: settings.i("searches"),
                    tables: settings.i("tables"),
                    docs: settings.i("docs"),
                    table_rows: settings.i("table_rows"),
                    table_cols: settings.i("table_cols"),
                    doc_sections: settings.i("doc_sections"),
                    doc_words: settings.i("doc_words"),
                    parts_table: settings.i("parts_table"),
                    parts_doc: settings.i("parts_doc"),
                };
                let ts = tasks::generate(&tp, i as i64);
                let mut o = Obj::empty();
                o.data = ts.store.clone();
                o.labels.insert("З".into());
                objects.insert(format!("задания{i}"), o);
                tasks.push(ts);
            }
        }
        let contours = [Contour::new(make_canary(&settings, 1), 1), Contour::new(make_canary(&settings, 2), 2)];
        World { cfg, settings, secret, journal, objects, contours, tasks, recv: Receivers::new(dir), own_refusal: false }
    }

    /// `new`: папка запуска, секрет, `outside.bin`, первые две записи.
    pub fn create(dir: &Path, kind6: bool, settings: Settings) -> World {
        fs::create_dir_all(dir).expect("создать папку запуска");
        let secret: [u8; 32] = if settings.s("fixed_secret").is_empty() {
            random_bytes(32).try_into().unwrap()
        } else {
            crate::json::unhex(settings.s("fixed_secret")).unwrap().try_into().unwrap()
        };
        fs::write(dir.join("secret.bin"), secret).expect("secret.bin");
        fs::write(dir.join("outside.bin"), b"").expect("outside.bin");
        // Файлы получателей создаются пустыми сразу: обвязка читает их и до первой доставки.
        for name in ["выход1", "выход2", "E1", "E2"] {
            fs::write(dir.join(format!("{name}.jsonl")), b"").expect("файл получателя");
        }
        let journal = Journal::create(&dir.join("journal5.jsonl"));
        let mut w = World::build(dir, kind6, settings, secret, journal);
        w.emit(
            rec(json!({"kind": "открыт", "defects": [], "limits": {"fuel": FUEL, "memory_pages": 2,
                "instances": MAX_INSTANCES, "object_bytes": OBJECT_BYTES, "table_entries": TABLE_ENTRIES,
                "price": 1000}})),
            None,
        );
        let mut r = w.settings.for_journal();
        r.insert("kind".into(), Value::from("учёт"));
        w.emit(r, None);
        w
    }

    /// `recover` и `restart` (§10.6).
    pub fn recover(dir: &Path, kind6: bool, reason: &str) -> Result<World, String> {
        let (journal, recs) = Journal::open(&dir.join("journal5.jsonl"))?;
        let Some(first) = recs.iter().find(|r| r.get("kind").and_then(Value::as_str) == Some("учёт")) else {
            return Err("журнал испорчен: нет записи «учёт»".into());
        };
        let mut m = first.clone();
        for k in ["kind", "seq", "prev", "hash"] {
            m.remove(k);
        }
        let settings = Settings::parse(&Value::Object(m), kind6).map_err(|e| format!("журнал испорчен: {e}"))?;
        let sb = fs::read(dir.join("secret.bin")).map_err(|e| format!("secret.bin: {e}"))?;
        let secret: [u8; 32] = sb.try_into().map_err(|_| "secret.bin: не 32 байта".to_string())?;
        let mut w = World::build(dir, kind6, settings, secret, journal);
        for r in &recs {
            w.apply(r);
        }
        for name in ["выход1", "выход2", "E1", "E2"] {
            w.recv.load(name);
        }
        for ci in 0..2usize {
            let pending: Vec<(String, String)> =
                w.contours[ci].open_debits.iter().map(|d| (d.fix.clone(), d.receiver.clone())).collect();
            let mut first = true;
            for (fix, receiver) in pending {
                let name = format!("{receiver}{}", ci + 1);
                let kind = if w.recv.has_fix(&name, &fix) { "завершено" } else { "потеряно" };
                w.emit(rec(json!({"kind": kind, "contour": ci + 1, "fix": fix})), None);
                if first {
                    first = false;
                    fault::point("посреди восстановления");
                }
            }
        }
        let c = |ci: usize| {
            let k = &w.contours[ci];
            json!({"budget": w.cfg.budget - k.spent, "spent": k.spent, "level": k.computed.len()})
        };
        let body = json!({"kind": "восстановлено", "reason": reason, "contours": {"1": c(0), "2": c(1)}});
        w.emit(rec(body), None);
        Ok(w)
    }

    // ---- журнал

    /// Дописать запись и учесть её в производном состоянии.
    pub fn emit(&mut self, r: Rec, torn: Option<&str>) {
        let r = self.journal.append(r, torn);
        self.apply(&r);
    }

    fn apply(&mut self, r: &Rec) {
        // Запись «действие» поля contour не имеет: контур — по последней цифре
        // субъекта (а1, п2). Плата за работу среды (§15) — в расход этого контура.
        if r.get("kind").and_then(Value::as_str) == Some("действие") {
            let charge = r.get("charge").and_then(Value::as_i64).unwrap_or(0);
            let digit = r.get("subject").and_then(Value::as_str).and_then(|s| s.chars().last()).and_then(|d| d.to_digit(10));
            if let (true, Some(d @ 1..=2)) = (charge != 0, digit) {
                self.contours[(d - 1) as usize].spent += charge;
            }
            return;
        }
        let Some(c) = r.get("contour").and_then(Value::as_i64) else { return };
        if !(1..=2).contains(&c) {
            return;
        }
        let con = &mut self.contours[(c - 1) as usize];
        let kind = r.get("kind").and_then(Value::as_str).unwrap_or("");
        let fix = r.get("fix").and_then(Value::as_str).map(str::to_string);
        if let Some(n) = fix.as_ref().and_then(|f| f.rsplit('-').next()).and_then(|n| n.parse::<u64>().ok()) {
            con.fixno = con.fixno.max(n + 1);
        }
        let int = |k: &str| r.get(k).and_then(Value::as_i64).unwrap_or(0);
        let task = match (r.get("task").and_then(Value::as_i64), r.get("part").and_then(Value::as_i64)) {
            (Some(t), Some(p)) => Some((t, p)),
            _ => None,
        };
        match kind {
            "работа" => {
                con.spent += int("price");
                if r.get("first").and_then(Value::as_bool) == Some(true) {
                    con.computed.push(int("node"));
                }
            }
            "списание" => {
                con.spent += int("price");
                if int("price") > 0 {
                    con.paid_debits += 1;
                }
                con.open_debits.push(OpenDebit {
                    fix: fix.unwrap_or_default(),
                    receiver: r.get("receiver").and_then(Value::as_str).unwrap_or("").to_string(),
                    task,
                });
            }
            "выброс" => con.spent += int("price"),
            "отказ ворот" => con.spent += int("charge"),
            "отказ" => {
                con.spent += int("charge");
                if let (Some(tp), Some("ответ неверен")) = (task, r.get("why").and_then(Value::as_str)) {
                    con.wrong.insert(tp);
                }
            }
            "проверка" => {
                if r.get("payer").and_then(Value::as_str) == Some("агент") {
                    con.spent += int("price");
                } else {
                    con.env_checks += int("price");
                }
            }
            "завершено" | "потеряно" => {
                if let Some(pos) = con.open_debits.iter().position(|d| Some(&d.fix) == fix.as_ref()) {
                    let d = con.open_debits.remove(pos);
                    if kind == "завершено" {
                        if let Some(tp) = d.task {
                            con.accepted.insert(tp);
                        }
                    }
                }
            }
            _ => {}
        }
    }

    /// Запись «действие» (§8).
    pub fn action(&mut self, subject: &str, rule: &str, obj: Option<&str>, facts: Vec<Value>) {
        self.action_charged(subject, rule, obj, facts, 0);
    }

    /// Запись «действие» с платой за работу среды (§15): плата списывается с
    /// контура `ci` и пишется полем `charge`, только если она не ноль.
    pub fn action_charged(&mut self, subject: &str, rule: &str, obj: Option<&str>, facts: Vec<Value>, charge: i64) {
        self.action_with(subject, rule, obj, facts, charge, Vec::new());
    }

    /// То же с дополнительными полями записи (вопрос и ответ модели, §17).
    pub fn action_with(&mut self, subject: &str, rule: &str, obj: Option<&str>, facts: Vec<Value>, charge: i64, extra: Vec<(&str, Value)>) {
        let last_digit = |s: &str| s.chars().last().filter(char::is_ascii_digit);
        let (kind, relation) = match obj {
            None => ("-".to_string(), "-"),
            Some(o) => {
                let d = last_digit(o);
                let kind = if d.is_some() { o[..o.len() - 1].to_string() } else { o.to_string() };
                let rel = if subject == "человек" {
                    "-"
                } else if o == "резольвер" {
                    "служба"
                } else if d.is_none() {
                    "общий"
                } else if d == last_digit(subject) {
                    "свой"
                } else {
                    "чужой"
                };
                (kind, rel)
            }
        };
        let mut r = rec(json!({"kind": "действие", "subject": subject, "signature": [rule, kind, relation], "facts": facts}));
        if charge != 0 {
            r.insert("charge".into(), json!(charge));
        }
        for (k, v) in extra {
            r.insert(k.into(), v);
        }
        self.emit(r, None);
    }

    // ---- команды

    pub fn say(&mut self, i: usize, text: &str) {
        let name = format!("вход{i}");
        let o = self.objects.get_mut(&name).unwrap();
        let room = OBJECT_BYTES.saturating_sub(o.data.len());
        let b = text.as_bytes();
        o.data.extend_from_slice(&b[..b.len().min(room)]);
        o.labels.insert("Ч".into());
        self.action("человек", "слова человека", Some(&name), vec![json!(["сведения", name, "Ч"])]);
    }

    pub fn log_start(&mut self, ci: usize, rights: &[(String, String)]) {
        let r = rec(json!({"kind": "начало", "contour": ci + 1, "subject": format!("а{}", ci + 1),
            "rights": rights.iter().map(pair_json).collect::<Vec<_>>()}));
        self.emit(r, None);
    }

    pub fn log_load_failure(&mut self, ci: usize, kind: &str, reason: &str) {
        self.emit(rec(json!({"kind": kind, "contour": ci + 1, "reason": reason})), None);
    }

    /// Выгрузка (§12). `memories[ci]` — память каждого экземпляра, у кого
    /// она есть; `fuel[ci]` — остаток топлива.
    pub fn dump(&self, memories: &[Vec<Vec<u8>>; 2], fuel: [u64; 2]) -> Value {
        let mut objs = Map::new();
        for (name, o) in &self.objects {
            objs.insert(
                name.clone(),
                json!({"data": bytes_json(&o.data), "labels": o.labels.iter().collect::<Vec<_>>(),
                    "refs": o.refs.iter().map(pair_json).collect::<Vec<_>>()}),
            );
        }
        let mut cs = Map::new();
        for ci in 0..2usize {
            let c = &self.contours[ci];
            let mut m = Map::new();
            m.insert("canary".into(), bytes_json(&c.canary));
            m.insert("labels".into(), json!(c.labels.iter().collect::<Vec<_>>()));
            m.insert("memories".into(), Value::Array(memories[ci].iter().map(|b| bytes_json(b)).collect()));
            m.insert(
                "tables".into(),
                Value::Array(
                    c.insts
                        .iter()
                        .map(|t| json!({"subject": t.subject, "rights": t.table.iter().map(pair_json).collect::<Vec<_>>()}))
                        .collect(),
                ),
            );
            m.insert("fuel".into(), json!(fuel[ci]));
            let mut ref_m = Map::new();
            for (k, v) in &c.refusals {
                ref_m.insert(k.to_string(), json!(v));
            }
            m.insert("refusals".into(), Value::Object(ref_m));
            m.insert("budget".into(), json!(self.cfg.budget - c.spent));
            m.insert("spent".into(), json!(c.spent));
            m.insert("level".into(), json!(c.computed.len()));
            m.insert("slots".into(), json!(c.slots.keys().collect::<Vec<_>>()));
            m.insert("helper_slots".into(), json!([]));
            m.insert("snap_slots".into(), json!([]));
            m.insert("fixno".into(), json!(c.fixno));
            if self.cfg.kind6 {
                m.insert("accepted".into(), json!(c.accepted.iter().map(|(t, p)| json!([t, p])).collect::<Vec<_>>()));
                m.insert("env_checks".into(), json!(c.env_checks));
            }
            cs.insert((ci + 1).to_string(), Value::Object(m));
        }
        json!({"objects": objs, "contours": cs, "outside_labels": ["С"]})
    }

    pub fn node_hex(&self, ci: usize, j: i64) -> String {
        hex(&self.contours[ci].slots[&j])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kanarejki_vektor() {
        let s = Settings::parse(&json!({"fixed_canary": "01"}), false).unwrap();
        assert_eq!(hex(&make_canary(&s, 1)), "9dcf97a184f32623d11a73124ceb99a5");
        assert_eq!(hex(&make_canary(&s, 2)), "a12871fee210fb8619291eaea194581c");
    }

    #[test]
    fn nachalnaya_tablica() {
        assert_eq!(initial_table(1, false, false).len(), 7);
        assert_eq!(initial_table(1, false, true)[7].0, "модель1");
        assert_eq!(initial_table(2, true, false)[7], ("задания2".to_string(), "чт".to_string()));
    }
}
