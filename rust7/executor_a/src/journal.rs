//! Журнал: цепочка записей, дописываемая построчно, и его проверка.

use crate::canon::{canon, sha_hex};
use serde_json::{Map, Value};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;

pub const ZERO: &str = "0000000000000000000000000000000000000000000000000000000000000000";

pub struct Journal {
    file: File,
    pub seq: u64,
    prev: String,
}

impl Journal {
    pub fn create(path: &Path) -> std::io::Result<Journal> {
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(Journal { file, seq: 0, prev: ZERO.to_string() })
    }

    /// Открыть существующий журнал после проверки (`seq` и отпечаток
    /// последней записи берутся из `records`).
    pub fn reopen(path: &Path, records: &[Value]) -> std::io::Result<Journal> {
        let file = OpenOptions::new().append(true).open(path)?;
        let prev = records
            .last()
            .map(|r| r["hash"].as_str().unwrap_or(ZERO).to_string())
            .unwrap_or_else(|| ZERO.to_string());
        Ok(Journal { file, seq: records.len() as u64, prev })
    }

    /// Строка журнала для записи без служебных полей. Цепочка продвигается
    /// только вызовом `commit`.
    pub fn line(&self, rec: &Value) -> (Vec<u8>, String) {
        let mut m: Map<String, Value> = rec.as_object().cloned().unwrap_or_default();
        m.insert("seq".into(), self.seq.into());
        m.insert("prev".into(), self.prev.clone().into());
        let hash = sha_hex(canon(&Value::Object(m.clone())).as_bytes());
        m.insert("hash".into(), hash.clone().into());
        let mut s = canon(&Value::Object(m));
        s.push('\n');
        (s.into_bytes(), hash)
    }

    pub fn write_bytes(&mut self, b: &[u8]) {
        let _ = self.file.write_all(b);
        let _ = self.file.sync_all();
    }

    pub fn commit(&mut self, hash: String) {
        self.seq += 1;
        self.prev = hash;
    }
}

/// Прочитать и проверить журнал (§5). Хвост без `\n` отрезается и с диска.
pub fn read_checked(path: &Path) -> Result<Vec<Value>, String> {
    let mut data = std::fs::read(path).map_err(|e| format!("журнал испорчен: {e}"))?;
    let cut = data.iter().rposition(|b| *b == b'\n').map(|p| p + 1).unwrap_or(0);
    if cut != data.len() {
        data.truncate(cut);
        let f = OpenOptions::new().write(true).open(path).map_err(|e| e.to_string())?;
        f.set_len(cut as u64).map_err(|e| e.to_string())?;
        let _ = f.sync_all();
    }
    let text = String::from_utf8(data).map_err(|_| "журнал испорчен: не UTF-8".to_string())?;
    let mut out: Vec<Value> = Vec::new();
    let mut prev = ZERO.to_string();
    let body_text = text.strip_suffix('\n').unwrap_or("");
    let lines: Vec<&str> = if text.is_empty() { Vec::new() } else { body_text.split('\n').collect() };
    for (n, line) in lines.into_iter().enumerate() {
        let v: Value = serde_json::from_str(line).map_err(|_| format!("журнал испорчен: строка {n} не разбирается"))?;
        let obj = v.as_object().ok_or_else(|| format!("журнал испорчен: строка {n} не объект"))?;
        if obj.get("seq").and_then(|x| x.as_u64()) != Some(n as u64) {
            return Err(format!("журнал испорчен: seq строки {n}"));
        }
        if obj.get("prev").and_then(|x| x.as_str()) != Some(prev.as_str()) {
            return Err(format!("журнал испорчен: prev строки {n}"));
        }
        let mut body = obj.clone();
        let hash = body.remove("hash");
        let want = sha_hex(canon(&Value::Object(body)).as_bytes());
        if hash.as_ref().and_then(|h| h.as_str()) != Some(want.as_str()) {
            return Err(format!("журнал испорчен: hash строки {n}"));
        }
        prev = want;
        out.push(v);
    }
    Ok(out)
}
