//! Журнал `journal5.jsonl`: цепочка отпечатков, дописывание, открытие при
//! восстановлении (контракт, §4, §5).

use crate::fault;
use crate::json::{canon, hex};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::Path;

pub const ZERO: &str = "0000000000000000000000000000000000000000000000000000000000000000";

pub struct Journal {
    file: File,
    seq: u64,
    prev: String,
}

/// Дописать строку целиком и сбросить на диск. Если назначен сбой по точке
/// `torn`, на диск попадает первая половина байтов строки, и процесс
/// завершается.
pub fn append_line(file: &mut File, line: &[u8], torn: Option<&str>) {
    if let Some(p) = torn {
        if fault::hit(p) {
            let _ = file.write_all(&line[..line.len() / 2]);
            let _ = file.sync_all();
            fault::crash();
        }
    }
    file.write_all(line).expect("запись файла");
    file.sync_all().expect("fsync");
}

fn open_append(path: &Path) -> File {
    OpenOptions::new().create(true).append(true).open(path).expect("открыть файл")
}

impl Journal {
    pub fn create(path: &Path) -> Journal {
        Journal { file: open_append(path), seq: 0, prev: ZERO.to_string() }
    }

    /// Дописать запись. Возвращает запись с `seq`, `prev`, `hash`.
    pub fn append(&mut self, mut rec: Map<String, Value>, torn: Option<&str>) -> Map<String, Value> {
        rec.insert("seq".into(), Value::from(self.seq));
        rec.insert("prev".into(), Value::String(self.prev.clone()));
        let h = hex(&Sha256::digest(canon(&Value::Object(rec.clone())).as_bytes()));
        rec.insert("hash".into(), Value::String(h.clone()));
        let mut line = canon(&Value::Object(rec.clone())).into_bytes();
        line.push(b'\n');
        append_line(&mut self.file, &line, torn);
        self.seq += 1;
        self.prev = h;
        rec
    }

    /// Открыть существующий журнал: отрезать оборванный хвост (физически) и
    /// проверить цепочку. `Err` — «журнал испорчен».
    pub fn open(path: &Path) -> Result<(Journal, Vec<Map<String, Value>>), String> {
        let mut raw = Vec::new();
        File::open(path)
            .and_then(|mut f| f.read_to_end(&mut raw))
            .map_err(|e| format!("журнал испорчен: не читается ({e})"))?;
        trim_tail(path, &mut raw);
        let mut recs = Vec::new();
        let mut prev = ZERO.to_string();
        if !raw.is_empty() {
            let body = &raw[..raw.len() - 1];
            for (n, line) in body.split(|b| *b == b'\n').enumerate() {
                let v: Value = serde_json::from_slice(line)
                    .map_err(|_| format!("журнал испорчен: запись {} не читается", n + 1))?;
                let Value::Object(mut m) = v else {
                    return Err(format!("журнал испорчен: запись {} не объект", n + 1));
                };
                let Some(Value::String(hash)) = m.remove("hash") else {
                    return Err(format!("журнал испорчен: у записи {} нет hash", n + 1));
                };
                let want = hex(&Sha256::digest(canon(&Value::Object(m.clone())).as_bytes()));
                let seq_ok = m.get("seq").and_then(Value::as_u64) == Some(n as u64);
                let prev_ok = m.get("prev").and_then(Value::as_str) == Some(prev.as_str());
                if !seq_ok || !prev_ok || hash != want {
                    return Err(format!("журнал испорчен: цепочка разорвана на записи {}", n + 1));
                }
                m.insert("hash".into(), Value::String(hash.clone()));
                prev = hash;
                recs.push(m);
            }
        }
        let j = Journal { file: open_append(path), seq: recs.len() as u64, prev };
        Ok((j, recs))
    }
}

/// Отрезать хвост после последнего `\n`, и в памяти, и на диске.
pub fn trim_tail(path: &Path, raw: &mut Vec<u8>) {
    let keep = raw.iter().rposition(|b| *b == b'\n').map_or(0, |p| p + 1);
    if keep != raw.len() {
        raw.truncate(keep);
        if let Ok(f) = OpenOptions::new().write(true).open(path) {
            let _ = f.set_len(keep as u64);
            let _ = f.sync_all();
        }
    }
}
