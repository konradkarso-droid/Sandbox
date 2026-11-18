//! Связь с движком wasmi: загрузка модуля, запуск, помощники.
//!
//! У каждого контура своё хранилище движка (`Store`): топливо и пределы
//! общие для агента и его помощников. Логика ворот остаётся в `World`;
//! здесь только перевод между памятью экземпляра и ею.

use crate::gate::{Halt, Mem};
use crate::world::{FUEL, MAX_INSTANCES, World, initial_table};
use std::cell::RefCell;
use wasmi::errors::ErrorKind;
use wasmi::{
    Caller, Config, Engine, Error, Extern, ExternType, Instance, Linker, Memory, Module, Store, StoreLimits,
    StoreLimitsBuilder, TrapCode, ValType,
};

thread_local! {
    static WORLD: RefCell<Option<World>> = const { RefCell::new(None) };
}

pub fn set_world(w: Option<World>) {
    WORLD.with(|x| *x.borrow_mut() = w);
}

pub fn has_world() -> bool {
    WORLD.with(|x| x.borrow().is_some())
}

pub fn with_world<R>(f: impl FnOnce(&mut World) -> R) -> R {
    WORLD.with(|x| f(x.borrow_mut().as_mut().expect("нет мира")))
}

/// Данные хранилища: экземпляры контура по номерам в таблице экземпляров.
pub struct Ctx {
    limits: StoreLimits,
    module: Option<Module>,
    pub instances: Vec<Option<Instance>>,
}

pub fn engine() -> Engine {
    let mut cfg = Config::default();
    cfg.consume_fuel(true);
    Engine::new(&cfg)
}

/// Хранилище контура. Пределы: память — 2 страницы на каждую (рост сверх
/// даёт −1 у `memory.grow`), не больше 4 памятей и 4 таблиц. Число
/// экземпляров считает `World`: движку оно задано с запасом, чтобы
/// брошенный неудачный экземпляр не съедал место.
pub fn new_store(engine: &Engine) -> Store<Ctx> {
    let limits = StoreLimitsBuilder::new()
        .memory_size(2 * 65536)
        .memories(MAX_INSTANCES)
        .tables(MAX_INSTANCES)
        .table_elements(1 << 20)
        .instances(64)
        .build();
    let mut s = Store::new(engine, Ctx { limits, module: None, instances: Vec::new() });
    s.limiter(|c| &mut c.limits);
    s.set_fuel(FUEL).expect("топливо");
    s
}

struct CallerMem<'a, 'b> {
    caller: &'a mut Caller<'b, Ctx>,
    mem: Option<Memory>,
}

impl Mem for CallerMem<'_, '_> {
    fn len(&self) -> Option<usize> {
        self.mem.map(|m| m.data_size(&*self.caller))
    }
    fn read(&self, at: usize, n: usize) -> Vec<u8> {
        self.mem.unwrap().data(&*self.caller)[at..at + n].to_vec()
    }
    fn write(&mut self, at: usize, data: &[u8]) {
        self.mem.unwrap().write(&mut *self.caller, at, data).expect("запись в память агента");
    }
}

/// «memory» — экспорт, который действительно память.
fn exported_memory(ext: Option<Extern>) -> Option<Memory> {
    ext.and_then(Extern::into_memory)
}

fn define(linker: &mut Linker<Ctx>, ci: usize, idx: usize) {
    linker
        .func_wrap("env", "call", move |mut caller: Caller<'_, Ctx>, num: i32, op: i32, a: i32, b: i32| -> Result<i32, Error> {
            let mem = exported_memory(caller.get_export("memory"));
            let mut m = CallerMem { caller: &mut caller, mem };
            match with_world(|w| w.gate_call(ci, idx, &mut m, num, op, a, b)) {
                Ok(r) => Ok(r),
                Err(h) => Err(halt_error(h)),
            }
        })
        .expect("env.call");
    linker
        .func_wrap("env", "spawn", move |mut caller: Caller<'_, Ctx>| -> Result<i32, Error> {
            spawn(&mut caller, ci, idx)
        })
        .expect("env.spawn");
}

fn put_instance(ctx: &mut Ctx, idx: usize, inst: Instance) {
    if ctx.instances.len() <= idx {
        ctx.instances.resize(idx + 1, None);
    }
    ctx.instances[idx] = Some(inst);
}

/// Канарейка — в начало экспортированной памяти, после стартовой функции.
fn write_canary(store: &mut impl wasmi::AsContextMut, inst: &Instance, canary: &[u8; 16]) {
    if let Some(m) = exported_memory(inst.get_export(&*store, "memory")) {
        if m.data_size(&*store) >= 16 {
            m.write(&mut *store, 0, canary).expect("канарейка");
        }
    }
}

fn spawn(caller: &mut Caller<'_, Ctx>, ci: usize, idx_x: usize) -> Result<i32, Error> {
    if let Some(h) = with_world(|w| w.attempt_blocked(ci, idx_x)) {
        return Err(halt_error(h));
    }
    let new_idx = match with_world(|w| w.spawn_begin(ci, idx_x)) {
        Ok(k) => k,
        Err(code) => return Ok(code),
    };
    let module = caller.data().module.clone().expect("модуль");
    let mut linker = Linker::new(caller.engine());
    define(&mut linker, ci, new_idx);
    let inst = match linker.instantiate_and_start(&mut *caller, &module) {
        Ok(i) => i,
        Err(e) => {
            with_world(|w| w.spawn_abort(ci, new_idx));
            caller.data_mut().instances.truncate(new_idx);
            // Нечем платить в стартовой функции этого помощника: создание
            // оборвано, плата вернулась, вызов кончается отказом −3.
            if matches!(e.downcast_ref::<SpawnAborted>(), Some(SpawnAborted(k)) if *k == new_idx) {
                return Ok(with_world(|w| w.refuse_spawn(ci, idx_x, -3)));
            }
            return Err(e);
        }
    };
    put_instance(caller.data_mut(), new_idx, inst);
    let canary = with_world(|w| w.contours[ci].canary);
    write_canary(&mut *caller, &inst, &canary);
    with_world(|w| w.spawn_finish(ci, idx_x, new_idx));
    if let Ok(f) = inst.get_typed_func::<(), ()>(&*caller, "helper") {
        f.call(&mut *caller, ())?;
    }
    Ok(0)
}

/// Ловушка правила «попытка платит»: контур остановлен, потому что нечем
/// платить. Отличается от ловушек движка, чтобы исход не путали с «топливо
/// кончилось»: это разные ресурсы.
#[derive(Debug)]
struct BudgetExhausted;

impl std::fmt::Display for BudgetExhausted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("бюджет исчерпан")
    }
}

impl wasmi::errors::HostError for BudgetExhausted {}

/// Обрыв создания помощника с этим номером: нечем платить в его стартовой функции.
#[derive(Debug)]
struct SpawnAborted(usize);

impl std::fmt::Display for SpawnAborted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("создание помощника оборвано: бюджет исчерпан")
    }
}

impl wasmi::errors::HostError for SpawnAborted {}

fn halt_error(h: Halt) -> Error {
    match h {
        Halt::Stop => Error::host(BudgetExhausted),
        Halt::AbortSpawn(k) => Error::host(SpawnAborted(k)),
    }
}

/// Вид ловушки (§7).
pub fn trap_kind(e: &Error) -> String {
    if e.downcast_ref::<BudgetExhausted>().is_some() {
        return "бюджет исчерпан".into();
    }
    match e.as_trap_code() {
        Some(TrapCode::OutOfFuel) => "топливо кончилось".into(),
        Some(TrapCode::StackOverflow) => "стек исчерпан".into(),
        Some(TrapCode::MemoryOutOfBounds) => "обращение за границу памяти".into(),
        Some(TrapCode::UnreachableCodeReached) => "unreachable".into(),
        _ => e.to_string(),
    }
}

/// Модуль берёт ровно две функции: env.call (4×i32 → i32) и env.spawn (() → i32).
fn check_imports(module: &Module) -> Result<(), String> {
    let call = ([ValType::I32; 4].as_slice(), [ValType::I32].as_slice());
    let spawn = ([].as_slice(), [ValType::I32].as_slice());
    for imp in module.imports() {
        let want = match (imp.module(), imp.name()) {
            ("env", "call") => call,
            ("env", "spawn") => spawn,
            (m, n) => return Err(format!("неописанный импорт {m}.{n}")),
        };
        match imp.ty() {
            ExternType::Func(ft) if ft.params() == want.0 && ft.results() == want.1 => {}
            _ => return Err(format!("импорт {}.{} не той подписи или вида", imp.module(), imp.name())),
        }
    }
    Ok(())
}

/// `load` (§7): исход и причина.
pub fn load(store: &mut Store<Ctx>, engine: &Engine, ci: usize, bytes: &[u8], kind6: bool) -> (String, String) {
    if with_world(|w| !w.contours[ci].insts.is_empty()) {
        return ("отклонён".into(), "агент уже загружен".into());
    }
    let reject = |reason: String| {
        with_world(|w| w.log_load_failure(ci, "загрузка отклонена", &reason));
        ("отклонён".to_string(), reason)
    };
    let module = match Module::new(engine, bytes) {
        Ok(m) => m,
        Err(e) => return reject(e.to_string()),
    };
    if let Err(reason) = check_imports(&module) {
        return reject(reason);
    }
    store.data_mut().module = Some(module.clone());
    let table = initial_table(ci + 1, kind6, with_world(|w| w.cfg.model));
    with_world(|w| w.contours[ci].insts.push(crate::world::Inst { subject: format!("а{}", ci + 1), table: table.clone() }));
    let mut linker = Linker::new(engine);
    define(&mut linker, ci, 0);
    match linker.instantiate_and_start(&mut *store, &module) {
        Ok(inst) => {
            put_instance(store.data_mut(), 0, inst);
            let canary = with_world(|w| w.contours[ci].canary);
            write_canary(&mut *store, &inst, &canary);
            // Права — таблица агента после стартовой функции, вместе с взятыми в ней (§14).
            with_world(|w| {
                let rights = w.contours[ci].insts[0].table.clone();
                w.log_start(ci, &rights)
            });
            ("загружен".into(), String::new())
        }
        Err(e) => {
            with_world(|w| w.contours[ci].insts.clear());
            store.data_mut().instances.clear();
            let reason = trap_kind(&e);
            match e.kind() {
                ErrorKind::Memory(_) | ErrorKind::Table(_) | ErrorKind::Linker(_) | ErrorKind::Instantiation(_) => reject(reason),
                _ => {
                    with_world(|w| w.log_load_failure(ci, "загрузка: ловушка", &reason));
                    ("ловушка".into(), reason)
                }
            }
        }
    }
}

/// `run` (§7).
pub fn run(store: &mut Store<Ctx>, ci: usize) -> (String, String) {
    if with_world(|w| w.contours[ci].insts.is_empty()) {
        return ("нет агента".into(), String::new());
    }
    let Some(inst) = store.data().instances.first().copied().flatten() else {
        return ("нет агента".into(), String::new());
    };
    let Ok(main) = inst.get_typed_func::<(), ()>(&*store, "main") else {
        return ("нет main".into(), String::new());
    };
    match main.call(&mut *store, ()) {
        Ok(()) => ("завершён".into(), String::new()),
        Err(e) => ("ловушка".into(), trap_kind(&e)),
    }
}

/// Память каждого экземпляра, у кого она есть, в порядке создания.
pub fn memories(store: &Store<Ctx>) -> Vec<Vec<u8>> {
    store
        .data()
        .instances
        .iter()
        .flatten()
        .filter_map(|inst| exported_memory(inst.get_export(store, "memory")))
        .map(|m| m.data(store).to_vec())
        .collect()
}
