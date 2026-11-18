//! Настройки, создание запуска (`new`), восстановление (`recover`,
//! `restart`) и выгрузка (`dump`).

use crate::canon::unhex;
use crate::core::{core, Contour, Core, Obj, St, POINTS};
use crate::engine;
use crate::journal::{self, Journal};
use crate::tasks::{self, TaskParams};
use base64::Engine as _;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::PathBuf;

#[derive(Clone, Copy, PartialEq)]
enum Ty {
    Int,
    Str,
}

/// (имя, тип, по умолчанию, только для шага 6). Порядок — как в контракте.
fn fields() -> Vec<(&'static str, Ty, Value, bool)> {
    use Ty::*;
    vec![
        ("budget", Int, json!(40), false),
        ("price", Int, json!(1), false),
        ("fail_price", Int, json!(1), false),
        ("work_price", Int, json!(1), false),
        ("graph", Str, json!("M3т"), false),
        ("levels", Int, json!(4), false),
        ("memory", Int, json!(4), false),
        ("fixed_secret", Str, json!(""), false),
        ("fixed_canary", Str, json!(""), false),
        ("rule", Str, json!("M3"), true),
        ("task_seed", Int, json!(0), true),
        ("sorts", Int, json!(1), true),
        ("searches", Int, json!(2), true),
        ("tables", Int, json!(1), true),
        ("docs", Int, json!(1), true),
        ("table_rows", Int, json!(8), true),
        ("table_cols", Int, json!(6), true),
        ("doc_sections", Int, json!(8), true),
        ("doc_words", Int, json!(10), true),
        ("parts_table", Int, json!(1), true),
        ("parts_doc", Int, json!(1), true),
        ("check_unit", Int, json!(64), true),
        ("canonical", Int, json!(1), true),
        ("task_rng", Str, json!("splitmix"), true),
    ]
}

/// Настройки по умолчанию (для `python` в task_rng — отказ, поэтому в
/// умолчании записан splitmix).
pub fn defaults(kind6: bool) -> BTreeMap<String, Value> {
    fields().into_iter().filter(|f| kind6 || !f.3).map(|f| (f.0.to_string(), f.2)).collect()
}

fn in_range(name: &str, v: i64) -> bool {
    match name {
        "levels" => (1..=20).contains(&v),
        "memory" | "sorts" | "searches" | "tables" | "docs" => (0..=255).contains(&v),
        "table_rows" | "table_cols" | "doc_sections" | "doc_words" => (1..=10_000).contains(&v),
        "parts_table" | "parts_doc" => (1..=255).contains(&v),
        "check_unit" => v >= 1,
        "canonical" => v == 0 || v == 1,
        _ => true,
    }
}

/// Разобрать настройки `new`; ошибка — причина отказа.
pub fn parse_settings(kind6: bool, v: &Value) -> Result<BTreeMap<String, Value>, String> {
    let mut cfg = defaults(kind6);
    if v.is_null() {
        return Ok(cfg);
    }
    let m = v.as_object().ok_or("настройки не объект")?;
    let fs = fields();
    for (k, x) in m {
        let f = fs.iter().find(|f| f.0 == k && (kind6 || !f.3)).ok_or_else(|| format!("неизвестное поле {k}"))?;
        match (f.1, x) {
            (Ty::Int, Value::Number(n)) if n.is_i64() => {
                if !in_range(k, n.as_i64().unwrap()) {
                    return Err(format!("значение поля {k} вне допустимого"));
                }
            }
            (Ty::Str, Value::String(s)) => match k.as_str() {
                "graph" if s != "M3т" && s != "M3ц" => return Err("неизвестный граф".into()),
                "rule" if !["M0", "M1", "M2", "M3"].contains(&s.as_str()) => return Err("неизвестный вариант".into()),
                "task_rng" if s != "splitmix" => return Err("генератор задач не поддержан".into()),
                "fixed_secret" if !s.is_empty() && !(s.len() == 64 && unhex(s).is_some()) => {
                    return Err("fixed_secret — 64 шестнадцатеричных знака".into())
                }
                "fixed_canary" if !s.is_empty() && unhex(s).is_none() => return Err("fixed_canary — шестнадцатеричная строка".into()),
                _ => {}
            },
            _ => return Err(format!("значение поля {k} не того типа")),
        }
        cfg.insert(k.clone(), x.clone());
    }
    if kind6 {
        let g = |k: &str| cfg[k].as_i64().unwrap();
        let total = g("sorts") + g("searches") + g("tables") * g("parts_table") + g("docs") * g("parts_doc");
        if total > 255 {
            return Err("частей задач больше 255".into());
        }
    }
    Ok(cfg)
}

pub fn initial_table(i: usize, kind6: bool) -> Vec<(String, String)> {
    let mut t = vec![
        (format!("снимок{i}"), "чт"),
        (format!("снимок{i}"), "зап"),
        (format!("выход{i}"), "зап"),
        (format!("E{i}"), "зап"),
        (format!("вход{i}"), "чт"),
        ("резольвер".to_string(), "выз"),
        (format!("узлы{i}"), "выз"),
    ];
    if kind6 {
        t.push((format!("задания{i}"), "чт"));
    }
    t.into_iter().map(|(o, r)| (o, r.to_string())).collect()
}

fn random_bytes<const N: usize>() -> [u8; N] {
    let mut b = [0u8; N];
    getrandom::getrandom(&mut b).expect("случайные байты");
    b
}

pub fn new_core(root: PathBuf, kind6: bool) -> Core {
    Core {
        kind6,
        root,
        engine: engine::new_engine(),
        started: false,
        run_no: 0,
        wd: PathBuf::new(),
        cfg: BTreeMap::new(),
        secret: [0; 32],
        journal: None,
        fault: None,
        counts: HashMap::new(),
        objects: BTreeMap::new(),
        cs: Vec::new(),
    }
}

fn task_params(c: &Core) -> TaskParams {
    let g = |k: &str| c.geti(k);
    TaskParams {
        seed: g("task_seed"),
        sorts: g("sorts") as u32,
        searches: g("searches") as u32,
        tables: g("tables") as u32,
        docs: g("docs") as u32,
        table_rows: g("table_rows") as u32,
        table_cols: g("table_cols") as u32,
        doc_sections: g("doc_sections") as u32,
        doc_words: g("doc_words") as u32,
        parts_table: g("parts_table") as u32,
        parts_doc: g("parts_doc") as u32,
    }
}

/// Канарейка: случайная или из `fixed_canary` — первые 16 байт
/// sha256(байты ‖ контур).
fn canary_of(fixed: &str, i: usize) -> [u8; 16] {
    if fixed.is_empty() {
        return random_bytes::<16>();
    }
    let mut b = unhex(fixed).unwrap();
    b.push(i as u8);
    let h = <sha2::Sha256 as sha2::Digest>::digest(&b);
    h[..16].try_into().unwrap()
}

/// Новые объекты и контуры по настройкам; память узлов пуста, агентов нет.
fn build_world(c: &mut Core) {
    c.objects.clear();
    c.cs.clear();
    let mk = || Obj { data: Vec::new(), labels: BTreeSet::new(), refs: Vec::new() };
    c.objects.insert("резольвер".into(), mk());
    let tp = task_params(c);
    let fixed = c.gets("fixed_canary");
    for i in 1..=2usize {
        for n in ["вход", "выход", "E", "снимок", "узлы"] {
            c.objects.insert(format!("{n}{i}"), mk());
        }
        let tasks = if c.kind6 {
            let t = tasks::generate(&tp, i);
            let mut o = mk();
            o.data = t.bytes.clone();
            o.labels.insert("З".into());
            c.objects.insert(format!("задания{i}"), o);
            Some(t)
        } else {
            None
        };
        let mut labels = BTreeSet::new();
        labels.insert(format!("А{i}"));
        c.cs.push(Contour {
            budget: c.cfg["budget"].as_i64().unwrap(),
            spent: 0,
            labels,
            canary: canary_of(&fixed, i),
            insts: Vec::new(),
            store: Some(engine::new_store(&c.engine)),
            module: None,
            agent: None,
            live: BTreeMap::new(),
            computed: 0,
            fixno: 0,
            refusals: BTreeMap::new(),
            accepted: BTreeSet::new(),
            wrong: BTreeSet::new(),
            env_checks: 0,
            debits_pos: 0,
            tasks,
        });
    }
}

fn parse_fault(v: &Value) -> Result<Option<(String, u64)>, String> {
    if v.is_null() {
        return Ok(None);
    }
    let a = v.as_array().filter(|a| a.len() == 2).ok_or("точка сбоя — [имя, номер]")?;
    let name = a[0].as_str().ok_or("имя точки не строка")?;
    let n = a[1].as_u64().filter(|n| *n >= 1).ok_or("номер прохода — целое ≥ 1")?;
    if !POINTS.contains(&name) {
        return Err(format!("неизвестная точка {name}"));
    }
    Ok(Some((name.to_string(), n)))
}

fn run_dir(c: &Core, n: u32) -> PathBuf {
    c.root.join(format!("run{n:05}"))
}

pub fn cmd_new(args: &[Value]) -> Result<Value, String> {
    if args.len() != 2 {
        return Err("new: два аргумента".into());
    }
    let kind6 = core(|c| c.kind6);
    let cfg = parse_settings(kind6, &args[0])?;
    let fault = parse_fault(&args[1])?;
    core(|c| {
        c.run_no += 1;
        c.wd = run_dir(c, c.run_no);
        std::fs::create_dir_all(&c.wd).map_err(|e| e.to_string())?;
        c.cfg = cfg;
        c.secret = match c.gets("fixed_secret").as_str() {
            "" => random_bytes::<32>(),
            s => unhex(s).unwrap().try_into().unwrap(),
        };
        std::fs::write(c.wd.join("secret.bin"), c.secret).map_err(|e| e.to_string())?;
        std::fs::write(c.wd.join("outside.bin"), b"").map_err(|e| e.to_string())?;
        // Обвязка читает файлы получателей сразу, поэтому они есть с первой минуты.
        for i in 1..=2 {
            for r in ["выход", "E"] {
                std::fs::write(c.recv_path(r, i), b"").map_err(|e| e.to_string())?;
            }
        }
        c.journal = Some(Journal::create(&c.wd.join("journal5.jsonl")).map_err(|e| e.to_string())?);
        c.fault = fault;
        c.counts.clear();
        c.started = true;
        build_world(c);
        c.log(
            json!({"kind": "открыт", "defects": [], "limits": {"fuel": 3_000_000, "memory_pages": 2, "instances": 4,
                   "object_bytes": 4096, "table_entries": 64, "price": 1000}}),
            None,
        );
        let mut rec = Map::new();
        rec.insert("kind".into(), json!("учёт"));
        for (k, v) in &c.cfg {
            rec.insert(k.clone(), v.clone());
        }
        c.log(Value::Object(rec), None);
        Ok(json!(c.wd.to_string_lossy()))
    })
}

fn recv_has(c: &Core, recv: &str, i: usize, fix: &str) -> bool {
    std::fs::read_to_string(c.recv_path(recv, i))
        .map(|t| {
            t.lines().any(|l| serde_json::from_str::<Value>(l).map_or(false, |v| v["fix"].as_str() == Some(fix)))
        })
        .unwrap_or(false)
}

fn cut_receivers(c: &Core) {
    for i in 1..=2 {
        for r in ["выход", "E"] {
            let p = c.recv_path(r, i);
            if let Ok(d) = std::fs::read(&p) {
                let cut = d.iter().rposition(|b| *b == b'\n').map(|k| k + 1).unwrap_or(0);
                if cut != d.len() {
                    if let Ok(f) = std::fs::OpenOptions::new().write(true).open(&p) {
                        let _ = f.set_len(cut as u64);
                        let _ = f.sync_all();
                    }
                }
            }
        }
    }
}

pub fn cmd_recover(args: &[Value], reason: &str) -> Result<Value, String> {
    if args.len() != 2 {
        return Err("два аргумента".into());
    }
    let n = args[0].as_u64().filter(|n| *n >= 1 && *n <= u32::MAX as u64).ok_or("номер запуска — целое ≥ 1")? as u32;
    let fault = parse_fault(&args[1])?;
    let wd = core(|c| run_dir(c, n));
    if !wd.is_dir() {
        return Err("папки запуска нет".into());
    }
    let recs = journal::read_checked(&wd.join("journal5.jsonl"))?;
    let kind6 = core(|c| c.kind6);
    let acct = recs
        .iter()
        .find(|r| r["kind"] == "учёт")
        .ok_or("журнал испорчен: нет записи учёта")?;
    let mut cfg = defaults(kind6);
    for (k, v) in cfg.clone() {
        if let Some(x) = acct.get(&k) {
            if std::mem::discriminant(x) == std::mem::discriminant(&v) {
                cfg.insert(k, x.clone());
            }
        }
    }
    core(|c| {
        c.run_no = c.run_no.max(n);
        c.wd = wd.clone();
        c.cfg = cfg;
        c.secret = std::fs::read(wd.join("secret.bin"))
            .ok()
            .and_then(|b| b.try_into().ok())
            .unwrap_or_else(|| match c.gets("fixed_secret").as_str() {
                "" => random_bytes::<32>(),
                s => unhex(s).unwrap().try_into().unwrap(),
            });
        if let Some(s) = Some(c.gets("fixed_secret")).filter(|s| !s.is_empty()) {
            c.secret = unhex(&s).unwrap().try_into().unwrap();
        }
        cut_receivers(c);
        c.journal = Some(Journal::reopen(&wd.join("journal5.jsonl"), &recs).map_err(|e| e.to_string())?);
        c.fault = fault;
        c.counts.clear();
        c.started = true;
        build_world(c);
        let mut summary = Map::new();
        for i in 1..=2usize {
            let mine: Vec<&Value> = recs.iter().filter(|r| r["contour"].as_u64() == Some(i as u64)).collect();
            let (mut spent, mut firsts, mut maxn, mut debits_pos, mut env) = (0i64, 0usize, -1i64, 0i64, 0i64);
            let mut closed: BTreeSet<String> = BTreeSet::new();
            let mut done: BTreeSet<String> = BTreeSet::new();
            let mut debits: Vec<&Value> = Vec::new();
            let mut wrong: BTreeSet<(u8, u8)> = BTreeSet::new();
            for r in &mine {
                let k = r["kind"].as_str().unwrap_or("");
                let price = r["price"].as_i64().unwrap_or(0);
                match k {
                    "работа" => {
                        spent += price;
                        if r["first"] == true {
                            firsts += 1;
                        }
                    }
                    "списание" => {
                        spent += price;
                        if price > 0 {
                            debits_pos += 1;
                        }
                        debits.push(r);
                    }
                    "выброс" => spent += price,
                    "отказ" => {
                        spent += r["charge"].as_i64().unwrap_or(0);
                        if r["why"] == "ответ неверен" {
                            if let (Some(t), Some(p)) = (r["task"].as_i64(), r["part"].as_i64()) {
                                wrong.insert((t as u8, p as u8));
                            }
                        }
                    }
                    "проверка" => {
                        if r["payer"] == "агент" {
                            spent += price;
                        } else {
                            env += price;
                        }
                    }
                    "завершено" => {
                        done.insert(r["fix"].as_str().unwrap_or("").to_string());
                        closed.insert(r["fix"].as_str().unwrap_or("").to_string());
                    }
                    "потеряно" => {
                        closed.insert(r["fix"].as_str().unwrap_or("").to_string());
                    }
                    _ => {}
                }
                if let Some(f) = r["fix"].as_str() {
                    if let Some(n) = f.split('-').nth(1).and_then(|x| x.parse::<i64>().ok()) {
                        maxn = maxn.max(n);
                    }
                }
            }
            {
                let ct = c.c(i);
                ct.budget -= spent;
                ct.spent = spent;
                ct.computed = firsts;
                ct.fixno = (maxn + 1) as u64;
                ct.debits_pos = debits_pos;
                ct.env_checks = env;
                ct.wrong = wrong;
            }
            let mut first_done = false;
            for d in debits {
                let fix = d["fix"].as_str().unwrap_or("").to_string();
                if closed.contains(&fix) {
                    continue;
                }
                let recv = d["receiver"].as_str().unwrap_or("выход").to_string();
                if recv_has(c, &recv, i, &fix) {
                    c.log(json!({"kind": "завершено", "contour": i, "fix": fix}), None);
                    done.insert(fix);
                } else {
                    c.log(json!({"kind": "потеряно", "contour": i, "fix": fix}), None);
                }
                if !first_done {
                    first_done = true;
                    c.point("посреди восстановления");
                }
            }
            let mut acc = BTreeSet::new();
            for d in recs.iter().filter(|r| r["contour"].as_u64() == Some(i as u64) && r["kind"] == "списание") {
                if let (Some(t), Some(p)) = (d["task"].as_i64(), d["part"].as_i64()) {
                    if done.contains(d["fix"].as_str().unwrap_or("")) {
                        acc.insert((t as u8, p as u8));
                    }
                }
            }
            let ct = c.c(i);
            ct.accepted = acc;
            summary.insert(i.to_string(), json!({"budget": ct.budget, "spent": ct.spent, "level": ct.computed}));
        }
        c.log(json!({"kind": "восстановлено", "reason": reason, "contours": Value::Object(summary)}), None);
        Ok(json!(c.wd.to_string_lossy()))
    })
}

pub fn b64(b: &[u8]) -> Value {
    json!({"b64": base64::engine::general_purpose::STANDARD.encode(b)})
}

pub fn cmd_dump() -> Value {
    let mut contours = Map::new();
    for i in 1..=2usize {
        let (mems, fuel) = engine::snapshot(i);
        let v = core(|c| {
            let kind6 = c.kind6;
            let ct = c.c(i);
            let tables: Vec<Value> = ct
                .insts
                .iter()
                .filter(|x| x.st == St::Live)
                .map(|x| json!({"subject": x.subject, "rights": x.table.iter().map(|(o, r)| json!([o, r])).collect::<Vec<_>>()}))
                .collect();
            let mut m = Map::new();
            m.insert("canary".into(), b64(&ct.canary));
            m.insert("labels".into(), json!(ct.labels.iter().collect::<Vec<_>>()));
            m.insert("memories".into(), Value::Array(mems.iter().map(|x| b64(x)).collect()));
            m.insert("tables".into(), Value::Array(tables));
            m.insert("fuel".into(), json!(fuel));
            let refs: Map<String, Value> = ct.refusals.iter().map(|(k, v)| (k.to_string(), json!(v))).collect();
            m.insert("refusals".into(), Value::Object(refs));
            m.insert("budget".into(), json!(ct.budget));
            m.insert("spent".into(), json!(ct.spent));
            m.insert("level".into(), json!(ct.computed));
            m.insert("slots".into(), json!(ct.live.keys().collect::<Vec<_>>()));
            m.insert("helper_slots".into(), json!([]));
            m.insert("snap_slots".into(), json!([]));
            m.insert("fixno".into(), json!(ct.fixno));
            if kind6 {
                m.insert("accepted".into(), json!(ct.accepted.iter().map(|(t, p)| json!([t, p])).collect::<Vec<_>>()));
                m.insert("env_checks".into(), json!(ct.env_checks));
            }
            Value::Object(m)
        });
        contours.insert(i.to_string(), v);
    }
    core(|c| {
        let mut objs = Map::new();
        for (name, o) in &c.objects {
            objs.insert(
                name.clone(),
                json!({"data": b64(&o.data), "labels": o.labels.iter().collect::<Vec<_>>(),
                       "refs": o.refs.iter().map(|(a, b)| json!([a, b])).collect::<Vec<_>>()}),
            );
        }
        json!({"objects": Value::Object(objs), "contours": Value::Object(contours), "outside_labels": ["С"]})
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canon::hex;

    #[test]
    fn canaries() {
        assert_eq!(hex(&canary_of("01", 1)), "9dcf97a184f32623d11a73124ceb99a5");
        assert_eq!(hex(&canary_of("01", 2)), "a12871fee210fb8619291eaea194581c");
    }
}
