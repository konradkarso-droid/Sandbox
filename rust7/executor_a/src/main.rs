mod canon;
mod core;
mod engine;
mod graph;
mod journal;
mod setup;
mod tasks;

use crate::core::{core, CORE};
use base64::Engine as _;
use serde_json::{json, Value};
use std::io::{BufRead, Write};

fn reply(status: &str, value: Value) {
    let line = serde_json::to_string(&json!({"status": status, "value": value})).unwrap();
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(line.as_bytes());
    let _ = out.write_all(b"\n");
    let _ = out.flush();
}

fn contour_arg(v: &Value) -> Result<usize, String> {
    match v.as_i64() {
        Some(n @ (1 | 2)) => Ok(n as usize),
        _ => Err("контур — 1 или 2".into()),
    }
}

fn started() -> Result<(), String> {
    if core(|c| c.started) {
        Ok(())
    } else {
        Err("сначала new".into())
    }
}

fn handle(cmd: &str, args: &[Value]) -> Result<Value, String> {
    let nargs = |n: usize| if args.len() == n { Ok(()) } else { Err(format!("{cmd}: {n} арг.")) };
    match cmd {
        "new" => setup::cmd_new(args),
        "recover" => setup::cmd_recover(args, "сбой"),
        "restart" => setup::cmd_recover(args, "перезапуск"),
        "load" => {
            nargs(2)?;
            let i = contour_arg(&args[0])?;
            let obj = args[1].as_object().filter(|o| o.len() == 1).ok_or("модуль — байты")?;
            let s = obj.get("b64").and_then(|s| s.as_str()).ok_or("модуль — байты")?;
            let bytes = base64::engine::general_purpose::STANDARD.decode(s).map_err(|_| "модуль: плохой base64".to_string())?;
            if bytes.len() > 1_048_576 {
                return Err("модуль длиннее 1 МиБ".into());
            }
            started()?;
            Ok(engine::load(i, &bytes))
        }
        "say" => {
            nargs(2)?;
            let i = contour_arg(&args[0])?;
            let t = args[1].as_str().filter(|t| t.chars().count() <= 4096).ok_or("текст — строка до 4096 знаков")?;
            started()?;
            core(|c| c.say(i, t));
            Ok(Value::Null)
        }
        "run" => {
            nargs(1)?;
            let i = contour_arg(&args[0])?;
            started()?;
            Ok(engine::run(i))
        }
        "dump" => {
            nargs(0)?;
            started()?;
            Ok(setup::cmd_dump())
        }
        "audit" => Ok(json!([])),
        _ => Err("неизвестная команда".into()),
    }
}

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    let arg = |k: &str| argv.iter().position(|a| a == k).and_then(|p| argv.get(p + 1)).cloned();
    let root = std::path::PathBuf::from(arg("--root").expect("--root"));
    let kind6 = match arg("--kind").as_deref() {
        Some("6") => true,
        Some("5б") => false,
        _ => {
            eprintln!("--kind: 5б или 6");
            std::process::exit(2);
        }
    };
    std::fs::create_dir_all(&root).ok();
    CORE.with(|c| *c.borrow_mut() = Some(setup::new_core(root, kind6)));
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let req: Result<Value, _> = serde_json::from_str(&line);
        let out = match &req {
            Ok(v) if v.is_object() => match (v["cmd"].as_str(), v["args"].as_array()) {
                (Some(cmd), Some(args)) => handle(cmd, args),
                _ => Err("запрос: cmd и args".to_string()),
            },
            _ => Err("запрос не JSON-объект".to_string()),
        };
        match out {
            Ok(v) => reply("ok", v),
            Err(e) => reply("отказ", json!(e)),
        }
    }
}
