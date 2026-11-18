//! Движок WebAssembly (wasmi): загрузка модуля, запуск, помощники.
//!
//! Ворота `env.call` и `env.spawn` — замыкания, знающие только номер
//! контура и номер экземпляра; всё остальное берётся из `core()`. Среда не
//! удерживается во время работы гостя.

use crate::core::{core, Contour, Data, Inst, Mem, St, FUEL, MAX_INST};
use serde_json::{json, Value};
use wasmi::{
    Caller, Config, Engine, Error, Extern, ExternType, Func, Instance, Linker, Memory, Module, Store, StoreLimitsBuilder,
    TrapCode, ValType,
};

pub fn new_engine() -> Engine {
    let mut cfg = Config::default();
    cfg.consume_fuel(true);
    Engine::new(&cfg)
}

pub fn new_store(engine: &Engine) -> Store<Data> {
    let limits = StoreLimitsBuilder::new()
        .memory_size(2 * 65536)
        .memories(MAX_INST)
        .tables(MAX_INST)
        .instances(64)
        .trap_on_grow_failure(false)
        .build();
    let mut s = Store::new(engine, Data { limits });
    s.limiter(|d| &mut d.limits);
    s.set_fuel(FUEL).expect("топливо");
    s
}

struct Guest<'a, 'b> {
    caller: &'a mut Caller<'b, Data>,
    mem: Memory,
}

impl Mem for Guest<'_, '_> {
    fn len(&self) -> usize {
        self.mem.data_size(&*self.caller)
    }
    fn read(&self, a: usize, b: usize) -> Vec<u8> {
        self.mem.data(&*self.caller)[a..a + b].to_vec()
    }
    fn write(&mut self, a: usize, d: &[u8]) {
        let _ = self.mem.write(&mut *self.caller, a, d);
    }
}

fn call_func(store: &mut impl wasmi::AsContextMut<Data = Data>, contour: usize, idx: usize) -> Func {
    Func::wrap(store, move |mut caller: Caller<'_, Data>, num: i32, op: i32, a: i32, b: i32| -> Result<i32, Error> {
        let mem = match caller.get_export("memory") {
            Some(Extern::Memory(m)) => Some(m),
            _ => None,
        };
        let r = match mem {
            Some(m) => {
                let mut g = Guest { caller: &mut caller, mem: m };
                core(|c| c.gate(contour, idx, Some(&mut g), num, op, a, b))
            }
            None => core(|c| c.gate(contour, idx, None, num, op, a, b)),
        };
        Ok(r)
    })
}

fn spawn_func(store: &mut impl wasmi::AsContextMut<Data = Data>, contour: usize, idx: usize) -> Func {
    Func::wrap(store, move |mut caller: Caller<'_, Data>| -> Result<i32, Error> { spawn(&mut caller, contour, idx) })
}

fn instantiate(
    store: &mut impl wasmi::AsContextMut<Data = Data>,
    module: &Module,
    contour: usize,
    idx: usize,
) -> Result<Instance, Error> {
    let mut linker: Linker<Data> = Linker::new(module.engine());
    let f = call_func(&mut *store, contour, idx);
    linker.define("env", "call", f)?;
    let g = spawn_func(&mut *store, contour, idx);
    linker.define("env", "spawn", g)?;
    linker.instantiate_and_start(store, module)
}

/// Канарейка — в начало экспортированной памяти `memory`, если она есть и
/// не короче 16 байт. Пишется после стартовой функции.
fn write_canary(store: &mut impl wasmi::AsContextMut<Data = Data>, inst: Instance, canary: [u8; 16]) {
    if let Some(m) = inst.get_memory(&*store, "memory") {
        if m.data_size(&*store) >= 16 {
            let _ = m.write(&mut *store, 0, &canary);
        }
    }
}

pub fn trap_kind(e: &Error) -> String {
    match e.as_trap_code() {
        Some(TrapCode::OutOfFuel) => "топливо кончилось".into(),
        Some(TrapCode::StackOverflow) => "стек исчерпан".into(),
        Some(TrapCode::MemoryOutOfBounds) => "обращение за границу памяти".into(),
        Some(TrapCode::UnreachableCodeReached) => "unreachable".into(),
        _ => format!("{e}"),
    }
}

fn spawn(caller: &mut Caller<'_, Data>, contour: usize, idx: usize) -> Result<i32, Error> {
    let prep = core(|c| {
        let ct = c.c(contour);
        let n = ct.insts.iter().filter(|x| x.st != St::Dead).count();
        if n >= MAX_INST {
            *ct.refusals.entry(-5).or_insert(0) += 1;
            return None;
        }
        let table = ct.insts[idx].table.clone();
        ct.insts.push(Inst { subject: format!("п{contour}"), table, instance: None, st: St::Building });
        Some((ct.insts.len() - 1, ct.module.clone().expect("модуль")))
    });
    let Some((nidx, module)) = prep else { return Ok(-5) };
    let inst = match instantiate(&mut *caller, &module, contour, nidx) {
        Ok(i) => i,
        Err(e) => {
            core(|c| c.c(contour).insts[nidx].st = St::Dead);
            if e.as_trap_code().is_some() {
                return Err(e);
            }
            core(|c| *c.c(contour).refusals.entry(-5).or_insert(0) += 1);
            return Ok(-5);
        }
    };
    let canary = core(|c| {
        let ct = c.c(contour);
        ct.insts[nidx].instance = Some(inst);
        ct.insts[nidx].st = St::Live;
        ct.canary
    });
    write_canary(&mut *caller, inst, canary);
    core(|c| {
        let subj = format!("п{contour}");
        let table = c.c(contour).insts[nidx].table.clone();
        c.action(&subj, contour, "создание помощника", None, vec![json!(["есть", subj])]);
        let facts: Vec<Value> = table.iter().map(|(o, r)| json!(["право", subj, o, r])).collect();
        c.action(&subj, contour, "копия прав помощнику", None, facts);
    });
    if let Ok(f) = inst.get_typed_func::<(), ()>(&*caller, "helper") {
        f.call(&mut *caller, ())?;
    }
    Ok(0)
}

fn reject(i: usize, reason: String) -> Value {
    core(|c| c.log(json!({"kind": "загрузка отклонена", "contour": i, "reason": reason}), None));
    json!(["отклонён", reason])
}

fn imports_ok(module: &Module) -> Result<(), String> {
    for imp in module.imports() {
        let ok = match (imp.module(), imp.name(), imp.ty()) {
            ("env", "call", ExternType::Func(t)) => {
                t.params() == [ValType::I32; 4] && t.results() == [ValType::I32]
            }
            ("env", "spawn", ExternType::Func(t)) => t.params().is_empty() && t.results() == [ValType::I32],
            _ => false,
        };
        if !ok {
            return Err(format!("импорт {}.{} не разрешён", imp.module(), imp.name()));
        }
    }
    Ok(())
}

pub fn load(i: usize, bytes: &[u8]) -> Value {
    if core(|c| c.c(i).agent.is_some()) {
        return json!(["отклонён", "агент уже загружен"]);
    }
    let engine = core(|c| c.engine.clone());
    let module = match Module::new(&engine, bytes) {
        Ok(m) => m,
        Err(e) => return reject(i, format!("{e}")),
    };
    if let Err(r) = imports_ok(&module) {
        return reject(i, r);
    }
    let (idx, mut store) = core(|c| {
        let ct: &mut Contour = c.c(i);
        ct.module = Some(module.clone());
        let table = crate::setup::initial_table(i, c.kind6);
        let ct = c.c(i);
        ct.insts.push(Inst { subject: format!("а{i}"), table, instance: None, st: St::Building });
        (ct.insts.len() - 1, ct.store.take().expect("хранилище"))
    });
    let res = instantiate(&mut store, &module, i, idx);
    let out = match res {
        Err(e) => {
            core(|c| c.c(i).insts[idx].st = St::Dead);
            let reason = trap_kind(&e);
            if e.as_trap_code().is_some() {
                core(|c| c.log(json!({"kind": "загрузка: ловушка", "contour": i, "reason": reason}), None));
                json!(["ловушка", reason])
            } else {
                core(|c| c.log(json!({"kind": "загрузка отклонена", "contour": i, "reason": reason}), None));
                json!(["отклонён", reason])
            }
        }
        Ok(inst) => {
            let canary = core(|c| {
                let ct = c.c(i);
                ct.insts[idx].instance = Some(inst);
                ct.insts[idx].st = St::Live;
                ct.agent = Some(idx);
                ct.canary
            });
            write_canary(&mut store, inst, canary);
            core(|c| {
                let rights: Vec<Value> = c.c(i).insts[idx].table.iter().map(|(o, r)| json!([o, r])).collect();
                c.log(json!({"kind": "начало", "contour": i, "subject": format!("а{i}"), "rights": rights}), None);
            });
            json!(["загружен", ""])
        }
    };
    core(|c| c.c(i).store = Some(store));
    out
}

pub fn run(i: usize) -> Value {
    let Some((idx, inst)) = core(|c| c.c(i).agent.map(|k| (k, c.c(i).insts[k].instance.unwrap()))) else {
        return json!(["нет агента", ""]);
    };
    let _ = idx;
    let mut store = core(|c| c.c(i).store.take().expect("хранилище"));
    let out = match inst.get_typed_func::<(), ()>(&store, "main") {
        Err(_) => json!(["нет main", ""]),
        Ok(f) => match f.call(&mut store, ()) {
            Ok(()) => json!(["завершён", ""]),
            Err(e) => json!(["ловушка", trap_kind(&e)]),
        },
    };
    core(|c| c.c(i).store = Some(store));
    out
}

/// (память экземпляров по порядку создания, остаток топлива).
pub fn snapshot(i: usize) -> (Vec<Vec<u8>>, u64) {
    core(|c| {
        let ct = c.c(i);
        let store = ct.store.as_ref().expect("хранилище");
        let mems = ct
            .insts
            .iter()
            .filter(|x| x.st == St::Live)
            .filter_map(|x| x.instance.and_then(|ins| ins.get_memory(store, "memory")))
            .map(|m| m.data(store).to_vec())
            .collect();
        (mems, store.get_fuel().unwrap_or(0))
    })
}
