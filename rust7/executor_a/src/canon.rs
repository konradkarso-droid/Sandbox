//! Канонический JSON журнала и отпечатки.
//!
//! Формат повторяет `json.dumps(x, ensure_ascii=False, sort_keys=True)`:
//! ключи по кодовым точкам, разделители «, » и «: », не-ASCII как есть.
//! Отпечаток записи — sha256 от этого текста; поэтому любое расхождение в
//! байтах ломает цепочку журнала.

use serde_json::Value;
use sha2::{Digest, Sha256};

pub fn escape_into(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => escape_into(s, out),
        Value::Array(a) => {
            out.push('[');
            for (k, x) in a.iter().enumerate() {
                if k > 0 {
                    out.push_str(", ");
                }
                write(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            // Порядок по кодовым точкам: байтовый порядок UTF-8 ему равен.
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (k, key) in keys.iter().enumerate() {
                if k > 0 {
                    out.push_str(", ");
                }
                escape_into(key, out);
                out.push_str(": ");
                write(&m[*key], out);
            }
            out.push('}');
        }
    }
}

pub fn canon(v: &Value) -> String {
    let mut s = String::new();
    write(v, &mut s);
    s
}

pub fn sha_hex(data: &[u8]) -> String {
    hex(&Sha256::digest(data))
}

pub fn hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{:02x}", x));
    }
    s
}

pub fn unhex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 || !s.is_ascii() {
        return None;
    }
    (0..s.len() / 2)
        .map(|k| u8::from_str_radix(&s[2 * k..2 * k + 2], 16).ok())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn work_record_hash() {
        let text = r#"{"contour": 1, "first": true, "kind": "работа", "node": 3, "preds": [1, 2], "prev": "0000000000000000000000000000000000000000000000000000000000000000", "price": 1, "seq": 5, "subject": "а1"}"#;
        let v: Value = serde_json::from_str(text).unwrap();
        assert_eq!(canon(&v), text);
        assert_eq!(
            sha_hex(canon(&v).as_bytes()),
            "51eb1d8121f5ca7b9fc620f4136c2b2af53ad64d3a300c8d136d49b1a9c81c37"
        );
    }

    #[test]
    fn escaping_hash() {
        let h = "7b22636861726765223a20302c20226b696e64223a2022d0bed182d0bad0b0d0b7222c20226d7367223a2022615c75303030315c225c5c5c6e5c74c3a9e280a8d091222c202278223a206e756c6c2c202279223a205b66616c73652c207b2261223a20322c202262223a20317d5d7d";
        let bytes = unhex(h).unwrap();
        let text = String::from_utf8(bytes.clone()).unwrap();
        let v: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(canon(&v).as_bytes(), &bytes[..]);
        assert_eq!(
            sha_hex(&bytes),
            "2685d299c1d36fed9dd89508d5a93e1c3ba682134097f4e4810d32044a264476"
        );
    }
}
