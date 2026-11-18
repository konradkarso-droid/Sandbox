//! Исполнитель шагов 5б и 6 на wasmi. Протокол — строки JSON (контракт, §1).

mod fault;
mod gate;
mod graph;
mod journal;
mod json;
mod model;
mod receivers;
mod runtime;
mod settings;
mod tasks;
mod world;

use json::json_bytes;
use serde_json::{Value, json};
use settings::Settings;
use std::io::{BufRead, Write};
use std::path::PathBuf;
use world::World;
use wasmi::{Engine, Store};

struct Exec {
    root: PathBuf,
    kind6: bool,
    run_counter: u64,
    engine: Engine,
    stores: Option<[Store<runtime::Ctx>; 2]>,
}

type Reply = (&'static str, Value);

fn ok(v: Value) -> Reply {
    ("ok", v)
}
fn refuse(why: &str) -> Reply {
    ("отказ", Value::from(why))
}

fn contour_arg(v: Option<&Value>) -> Option<usize> {
    match v.and_then(Value::as_i64) {
        Some(i @ 1..=2) => Some(i as usize - 1),
        _ => None,
    }
}

/// Точка сбоя: `null` или `[имя, номер ≥ 1]`.
fn fault_arg(v: Option<&Value>) -> Result<Option<(String, u64)>, String> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Array(a)) if a.len() == 2 => match (a[0].as_str(), a[1].as_u64()) {
            (Some(n), Some(k)) if k >= 1 => Ok(Some((n.to_string(), k))),
            _ => Err("точка сбоя: имя и номер прохода ≥ 1".into()),
        },
        _ => Err("точка сбоя: null или [имя, номер]".into()),
    }
}

impl Exec {
    fn run_dir(&self, n: u64) -> PathBuf {
        self.root.join(format!("run{n:05}"))
    }

    fn fresh_stores(&mut self) {
        self.stores = Some([runtime::new_store(&self.engine), runtime::new_store(&self.engine)]);
    }

    fn highest_existing(&self) -> u64 {
        std::fs::read_dir(&self.root)
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| e.file_name().to_str().and_then(|n| n.strip_prefix("run")?.parse::<u64>().ok()))
            .max()
            .unwrap_or(0)
    }

    fn handle(&mut self, req: &Value) -> Reply {
        let Some(cmd) = req.get("cmd").and_then(Value::as_str) else { return refuse("нет команды") };
        let empty = Vec::new();
        let args = match req.get("args") {
            None | Some(Value::Null) => &empty,
            Some(Value::Array(a)) => a,
            _ => return refuse("args не список"),
        };
        match cmd {
            "new" => {
                let settings = match Settings::parse(args.first().unwrap_or(&Value::Null), self.kind6) {
                    Ok(s) => s,
                    Err(e) => return refuse(&e),
                };
                if settings.i("model") != 0 && !model::connected() {
                    return refuse("model не 0, а модель при запуске исполнителя не подключена");
                }
                let plan = match fault_arg(args.get(1)) {
                    Ok(p) => p,
                    Err(e) => return refuse(&e),
                };
                self.run_counter = self.run_counter.max(self.highest_existing()) + 1;
                let dir = self.run_dir(self.run_counter);
                fault::arm(plan);
                runtime::set_world(None);
                runtime::set_world(Some(World::create(&dir, self.kind6, settings)));
                self.fresh_stores();
                ok(Value::from(dir.to_string_lossy().to_string()))
            }
            "recover" | "restart" => {
                let Some(n) = args.first().and_then(Value::as_u64).filter(|n| *n >= 1) else {
                    return refuse("номер запуска — целое ≥ 1");
                };
                let plan = match fault_arg(args.get(1)) {
                    Ok(p) => p,
                    Err(e) => return refuse(&e),
                };
                let dir = self.run_dir(n);
                if !dir.is_dir() {
                    return refuse("папки запуска нет");
                }
                fault::arm(plan);
                let reason = if cmd == "recover" { "сбой" } else { "перезапуск" };
                runtime::set_world(None);
                match World::recover(&dir, self.kind6, reason) {
                    Ok(w) => {
                        runtime::set_world(Some(w));
                        self.run_counter = self.run_counter.max(n);
                        self.fresh_stores();
                        ok(Value::from(dir.to_string_lossy().to_string()))
                    }
                    Err(e) => refuse(&e),
                }
            }
            "load" | "say" | "run" | "dump" | "audit" => self.in_run(cmd, args),
            _ => refuse("неизвестная команда"),
        }
    }

    fn in_run(&mut self, cmd: &str, args: &[Value]) -> Reply {
        if cmd == "audit" {
            return ok(json!([]));
        }
        if !runtime::has_world() {
            return refuse("сначала new");
        }
        if cmd == "dump" {
            let stores = self.stores.as_ref().unwrap();
            let mems = [runtime::memories(&stores[0]), runtime::memories(&stores[1])];
            let fuel = [stores[0].get_fuel().unwrap_or(0), stores[1].get_fuel().unwrap_or(0)];
            return ok(runtime::with_world(|w| w.dump(&mems, fuel)));
        }
        let Some(ci) = contour_arg(args.first()) else { return refuse("контур — 1 или 2") };
        match cmd {
            "load" => {
                let Some(bytes) = args.get(1).and_then(json_bytes).filter(|b| b.len() <= 1 << 20) else {
                    return refuse("модуль — байты не длиннее 1 048 576");
                };
                let stores = self.stores.as_mut().unwrap();
                let (o, r) = runtime::load(&mut stores[ci], &self.engine, ci, &bytes, self.kind6);
                ok(json!([o, r]))
            }
            "say" => {
                let Some(t) = args.get(1).and_then(Value::as_str).filter(|t| t.chars().count() <= 4096) else {
                    return refuse("текст — строка не длиннее 4096 знаков");
                };
                runtime::with_world(|w| w.say(ci + 1, t));
                ok(Value::Null)
            }
            _ => {
                let stores = self.stores.as_mut().unwrap();
                let (o, r) = runtime::run(&mut stores[ci], ci);
                ok(json!([o, r]))
            }
        }
    }
}

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    let mut root = None;
    let mut kind = None;
    let mut model_mode: Option<String> = None;
    let mut model_timeout = 120.0f64;
    let mut model_net = String::from("netns");
    let mut k = 1;
    while k < argv.len() {
        let arg = argv[k].as_str();
        // Параметры модели обвязка передаёт одним словом «--имя=значение»: режим сам
        // начинается с «--», и раздельное значение разборщик принял бы за новый параметр.
        // Раздельная запись тоже принимается.
        if let Some(v) = arg.strip_prefix("--model=") {
            model_mode = Some(v.to_string());
            k += 1;
            continue;
        }
        if let Some(v) = arg.strip_prefix("--model-net=") {
            model_net = v.to_string();
            k += 1;
            continue;
        }
        if let Some(v) = arg.strip_prefix("--model-timeout=") {
            model_timeout = v.parse().unwrap_or(f64::NAN);
            k += 1;
            continue;
        }
        match arg {
            "--root" => root = argv.get(k + 1).cloned(),
            "--kind" => kind = argv.get(k + 1).cloned(),
            "--model" => model_mode = argv.get(k + 1).cloned(),
            "--model-net" => model_net = argv.get(k + 1).cloned().unwrap_or_default(),
            "--model-timeout" => model_timeout = argv.get(k + 1).and_then(|v| v.parse().ok()).unwrap_or(f64::NAN),
            _ => {}
        }
        k += 2;
    }
    let (Some(root), Some(kind)) = (root, kind) else {
        eprintln!("запуск: executor7 --root ПАПКА --kind 5б|6");
        std::process::exit(2);
    };
    let kind6 = match kind.as_str() {
        "6" => true,
        "5б" => false,
        _ => {
            eprintln!("--kind: 5б или 6");
            std::process::exit(2);
        }
    };
    // Другое значение --model-net — отказ запуска и без --model; без --model способ не действует.
    let net = match model_net.as_str() {
        "netns" => model::Net::Netns,
        "probe" => model::Net::Probe,
        _ => {
            eprintln!("--model-net: netns или probe");
            std::process::exit(2);
        }
    };
    if let Some(mode) = model_mode {
        if let Err(e) = model::start(&mode, model_timeout, net) {
            eprintln!("модель: {e}");
            std::process::exit(2);
        }
    }
    std::fs::create_dir_all(&root).expect("папка ПАПКА");
    let mut ex = Exec { root: PathBuf::from(root), kind6, run_counter: 0, engine: runtime::engine(), stores: None };
    let stdin = std::io::stdin();
    let mut out = std::io::stdout().lock();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let (status, value) = match serde_json::from_str::<Value>(&line) {
            // Паника — это «сбой» (закон И27): её надо показать, а не потерять вместе с процессом.
            Ok(req) => match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| ex.handle(&req))) {
                Ok(r) => r,
                Err(_) => ("сбой", Value::from("внутренняя ошибка исполнителя")),
            },
            Err(_) => refuse("запрос — не JSON"),
        };
        let reply = json!({"status": status, "value": value});
        writeln!(out, "{}", reply).and_then(|_| out.flush()).expect("ответ");
    }
}
