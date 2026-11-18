//! Канонический JSON (контракт, §4) и байты в JSON (§1).
//!
//! Каноническая запись совпадает с `json.dumps(x, ensure_ascii=False,
//! sort_keys=True)` в Python. Свой писатель нужен потому, что
//! `serde_json` экранирует управляющие знаки иначе (например, `\u0008`
//! вместо `\b` у одних версий и наоборот у других), а отпечаток журнала
//! считается по байтам.

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Map, Value};

/// Каноническая строка JSON. Ключи объектов упорядочены по кодовым точкам:
/// `Map` без `preserve_order` — это `BTreeMap<String, _>`, а порядок строк
/// по байтам UTF-8 совпадает с порядком по кодовым точкам.
pub fn canon(v: &Value) -> String {
    let mut out = String::new();
    write_value(v, &mut out);
    out
}

fn write_value(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_str(s, out),
        Value::Array(a) => {
            out.push('[');
            for (k, x) in a.iter().enumerate() {
                if k > 0 {
                    out.push_str(", ");
                }
                write_value(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            out.push('{');
            for (k, (key, x)) in m.iter().enumerate() {
                if k > 0 {
                    out.push_str(", ");
                }
                write_str(key, out);
                out.push_str(": ");
                write_value(x, out);
            }
            out.push('}');
        }
    }
}

/// Строка в кавычках: экранируются только `"`, `\` и знаки с кодом меньше
/// 0x20; всё остальное, включая не-ASCII, — как есть.
pub fn write_str(s: &str, out: &mut String) {
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

/// Байты как объект `{"b64": "..."}`.
pub fn bytes_json(b: &[u8]) -> Value {
    let mut m = Map::new();
    m.insert("b64".into(), Value::String(STANDARD.encode(b)));
    Value::Object(m)
}

/// Обратное: `Some(байты)`, если значение — объект ровно с одним полем `b64`
/// и правильным base64 (стандартный алфавит, с `=`).
pub fn json_bytes(v: &Value) -> Option<Vec<u8>> {
    let m = v.as_object()?;
    if m.len() != 1 {
        return None;
    }
    let s = m.get("b64")?.as_str()?;
    STANDARD.decode(s).ok()
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
    use serde_json::json;
    use sha2::{Digest, Sha256};

    #[test]
    fn vektor_zapis_i_otpechatok() {
        let v = json!({"contour": 1, "first": true, "kind": "работа", "node": 3, "preds": [1, 2],
            "prev": "0000000000000000000000000000000000000000000000000000000000000000",
            "price": 1, "seq": 5, "subject": "а1"});
        let s = canon(&v);
        assert!(s.starts_with("{\"contour\": 1, \"first\": true, \"kind\": \"работа\""));
        assert_eq!(
            hex(&Sha256::digest(s.as_bytes())),
            "51eb1d8121f5ca7b9fc620f4136c2b2af53ad64d3a300c8d136d49b1a9c81c37"
        );
    }

    #[test]
    fn vektor_ekranirovanie() {
        let want = unhex("7b22636861726765223a20302c20226b696e64223a2022d0bed182d0bad0b0d0b7222c20226d7367223a2022615c75303030315c225c5c5c6e5c74c3a9e280a8d091222c202278223a206e756c6c2c202279223a205b66616c73652c207b2261223a20322c202262223a20317d5d7d").unwrap();
        let v = json!({"charge": 0, "kind": "отказ", "msg": "a\u{1}\"\\\n\té\u{2028}Б", "x": null,
            "y": [false, {"a": 2, "b": 1}]});
        let s = canon(&v);
        assert_eq!(s.as_bytes(), &want[..]);
        assert_eq!(
            hex(&Sha256::digest(&want)),
            "2685d299c1d36fed9dd89508d5a93e1c3ba682134097f4e4810d32044a264476"
        );
    }

    #[test]
    fn bayty_tuda_i_obratno() {
        let v = bytes_json(&[1, 2, 3, 255]);
        assert_eq!(json_bytes(&v).unwrap(), vec![1, 2, 3, 255]);
        assert!(json_bytes(&json!({"b64": "!!"})).is_none());
        assert!(json_bytes(&json!({"b64": "AA==", "x": 1})).is_none());
    }
}
