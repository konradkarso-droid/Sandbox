//! Получатели `выход{i}.jsonl` и `E{i}.jsonl` (контракт, §10.7).

use crate::journal::{append_line, trim_tail};
use crate::json::write_str;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::Read;
use std::path::{Path, PathBuf};

struct One {
    path: PathBuf,
    file: Option<File>,
    pairs: HashSet<(String, String)>,
    fixes: HashSet<String>,
}

pub struct Receivers {
    dir: PathBuf,
    by_name: HashMap<String, One>,
}

impl Receivers {
    pub fn new(dir: &Path) -> Receivers {
        Receivers { dir: dir.to_path_buf(), by_name: HashMap::new() }
    }

    /// Подгрузить существующий файл: оборванный хвост отрезается, пары
    /// (номер, текст) запоминаются для проверки повторной доставки.
    pub fn load(&mut self, name: &str) {
        let path = self.dir.join(format!("{name}.jsonl"));
        let mut one = One { path: path.clone(), file: None, pairs: HashSet::new(), fixes: HashSet::new() };
        let mut raw = Vec::new();
        if let Ok(mut f) = File::open(&path) {
            let _ = f.read_to_end(&mut raw);
            trim_tail(&path, &mut raw);
            for line in raw.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
                if let Ok(Value::Object(m)) = serde_json::from_slice::<Value>(line) {
                    if let Some(fix) = m.get("fix").and_then(Value::as_str) {
                        one.fixes.insert(fix.to_string());
                        for t in m.get("msgs").and_then(Value::as_array).into_iter().flatten() {
                            if let Some(t) = t.as_str() {
                                one.pairs.insert((fix.to_string(), t.to_string()));
                            }
                        }
                    }
                }
            }
        }
        self.by_name.insert(name.to_string(), one);
    }

    fn one(&mut self, name: &str) -> &mut One {
        if !self.by_name.contains_key(name) {
            self.load(name);
        }
        self.by_name.get_mut(name).unwrap()
    }

    pub fn has_fix(&mut self, name: &str, fix: &str) -> bool {
        self.one(name).fixes.contains(fix)
    }

    /// Принять (номер, текст): повторная пара ничего не пишет.
    pub fn accept(&mut self, name: &str, fix: &str, text: &str) {
        let one = self.one(name);
        if one.pairs.contains(&(fix.to_string(), text.to_string())) {
            return;
        }
        let mut line = String::from("{\"fix\": ");
        write_str(fix, &mut line);
        line.push_str(", \"msgs\": [");
        write_str(text, &mut line);
        line.push_str("]}\n");
        if one.file.is_none() {
            one.file = Some(OpenOptions::new().create(true).append(true).open(&one.path).expect("открыть получателя"));
        }
        append_line(one.file.as_mut().unwrap(), line.as_bytes(), Some("запись доставки оборвана"));
        one.pairs.insert((fix.to_string(), text.to_string()));
        one.fixes.insert(fix.to_string());
    }
}
