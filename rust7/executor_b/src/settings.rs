//! Настройки `new` (контракт, §2): разбор, значения по умолчанию, проверка.

use crate::json::unhex;
use serde_json::{Map, Value};

const COMMON: [(&str, i64); 11] = [
    ("model", 0),
    ("model_answer", 1024),
    ("model_byte_unit", 0),
    ("gate_price", 0),
    ("paid_attempts", 0),
    ("budget", 40),
    ("price", 1),
    ("fail_price", 1),
    ("work_price", 1),
    ("levels", 4),
    ("memory", 4),
];
const STR_COMMON: [(&str, &str); 3] = [("graph", "M3т"), ("fixed_secret", ""), ("fixed_canary", "")];
const INT6: [(&str, i64); 11] = [
    ("task_seed", 0),
    ("sorts", 1),
    ("searches", 2),
    ("tables", 1),
    ("docs", 1),
    ("table_rows", 8),
    ("table_cols", 6),
    ("doc_sections", 8),
    ("doc_words", 10),
    ("parts_table", 1),
    ("parts_doc", 1),
];
const INT6B: [(&str, i64); 3] = [("check_unit", 64), ("canonical", 1), ("check_price", 1)];
const STR6: [(&str, &str); 2] = [("rule", "M3"), ("task_rng", "python")];

/// Все поля настроек с значениями; порядок записи в журнал задаёт
/// канонический JSON, а не этот список.
#[derive(Clone)]
pub struct Settings {
    pub m: Map<String, Value>,
}

impl Settings {
    pub fn i(&self, k: &str) -> i64 {
        self.m[k].as_i64().unwrap()
    }
    pub fn s(&self, k: &str) -> &str {
        self.m[k].as_str().unwrap()
    }

    /// Поля «цены работы среды», «попытка платит» и «вопрос модели», которые в запись «учёт» попадают, только если
    /// не равны значению по умолчанию: так журнал при прежних ценах остаётся прежним.
    pub fn for_journal(&self) -> Map<String, Value> {
        let mut r = self.m.clone();
        for (k, d) in [
            ("gate_price", 0),
            ("check_price", 1),
            ("paid_attempts", 0),
            ("model", 0),
            ("model_answer", 1024),
            ("model_byte_unit", 0),
        ] {
            if r.get(k).and_then(Value::as_i64) == Some(d) {
                r.remove(k);
            }
        }
        r
    }

    fn defaults(kind6: bool) -> Map<String, Value> {
        let mut m = Map::new();
        for (k, v) in COMMON.iter() {
            m.insert((*k).into(), Value::from(*v));
        }
        for (k, v) in STR_COMMON {
            m.insert(k.into(), Value::from(v));
        }
        if kind6 {
            for (k, v) in INT6.iter().chain(INT6B.iter()) {
                m.insert((*k).into(), Value::from(*v));
            }
            for (k, v) in STR6 {
                m.insert(k.into(), Value::from(v));
            }
        }
        m
    }

    /// Разобрать настройки запроса `new`. `Err` — причина отказа.
    pub fn parse(arg: &Value, kind6: bool) -> Result<Settings, String> {
        let mut m = Self::defaults(kind6);
        match arg {
            Value::Null => {}
            Value::Object(o) => {
                for (k, v) in o {
                    let Some(def) = m.get(k) else {
                        return Err(format!("неизвестное поле настроек: {k}"));
                    };
                    let ok = if def.is_string() { v.is_string() } else { v.as_i64().is_some() };
                    if !ok {
                        return Err(format!("значение поля {k} не того вида"));
                    }
                    m.insert(k.clone(), v.clone());
                }
            }
            _ => return Err("настройки не объект".into()),
        }
        let s = Settings { m };
        s.validate(kind6)?;
        Ok(s)
    }

    /// Значения, с которыми исполнитель не может честно работать.
    fn validate(&self, kind6: bool) -> Result<(), String> {
        let bad = |what: &str| Err(format!("недопустимые настройки: {what}"));
        if !matches!(self.s("graph"), "M3т" | "M3ц") {
            return bad("graph");
        }
        if !(1..=16).contains(&self.i("levels")) {
            return bad("levels");
        }
        if self.i("memory") < 0 || self.i("memory") > 1 << 16 {
            return bad("memory");
        }
        let fs = self.s("fixed_secret");
        if !fs.is_empty() && !(fs.len() == 64 && unhex(fs).is_some()) {
            return bad("fixed_secret");
        }
        if self.s("fixed_canary").len() % 2 != 0 || unhex(self.s("fixed_canary")).is_none() {
            return bad("fixed_canary");
        }
        if self.i("gate_price") < 0 || self.i("gate_price") > 1 << 20 {
            return bad("gate_price");
        }
        if !matches!(self.i("paid_attempts"), 0 | 1) {
            return bad("paid_attempts");
        }
        // Правило «попытка платит» держится на том, что у каждой попытки плата
        // не меньше 1; с нулевой ценой число попыток контура не ограничено.
        if self.i("paid_attempts") == 1 {
            for k in ["gate_price", "price", "fail_price", "work_price"] {
                if self.i(k) < 1 {
                    return bad(&format!("paid_attempts = 1 требует {k} не меньше 1"));
                }
            }
        }
        if !matches!(self.i("model"), 0..=2) {
            return bad("model");
        }
        if !(1..=4096).contains(&self.i("model_answer")) {
            return bad("model_answer");
        }
        if self.i("model_byte_unit") < 0 {
            return bad("model_byte_unit");
        }
        if kind6 {
            if self.i("check_price") < 0 || self.i("check_price") > 1 << 20 {
                return bad("check_price");
            }
            if !matches!(self.s("rule"), "M0" | "M1" | "M2" | "M3") {
                return bad("rule");
            }
            if self.s("task_rng") != "splitmix" {
                return bad("task_rng: поддержан только splitmix");
            }
            for k in ["sorts", "searches", "tables", "docs"] {
                if !(0..=64).contains(&self.i(k)) {
                    return bad(k);
                }
            }
            if !(1..=255).contains(&self.i("table_cols")) || !(0..=4096).contains(&self.i("table_rows")) {
                return bad("размер таблицы");
            }
            if !(1..=64).contains(&self.i("doc_words")) || !(0..=4096).contains(&self.i("doc_sections")) {
                return bad("размер документа");
            }
            if !(1..=255).contains(&self.i("parts_table")) || !(1..=255).contains(&self.i("parts_doc")) {
                return bad("число частей");
            }
            if self.i("check_unit") < 1 {
                return bad("check_unit");
            }
            let parts = self.i("sorts")
                + self.i("searches")
                + self.i("tables") * self.i("parts_table")
                + self.i("docs") * self.i("parts_doc");
            let tasks = self.i("sorts") + self.i("searches") + self.i("tables") + self.i("docs");
            if parts > 255 || tasks > 255 {
                return bad("слишком много задач");
            }
        }
        Ok(())
    }
}
