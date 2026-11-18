//! Точки сбоя (контракт, §9): процесс завершается с кодом 17 на назначенном
//! проходе назначенной точки. Всё, что записано до этого, уже на диске:
//! файлы пишутся без буфера и сбрасываются сразу.

use std::cell::RefCell;
use std::collections::HashMap;

pub const CRASH_EXIT: i32 = 17;

#[derive(Default)]
struct State {
    plan: Option<(String, u64)>,
    passes: HashMap<String, u64>,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

/// Назначить новую точку сбоя и обнулить счёт проходов: счёт ведётся с
/// начала жизни исполнителя после `new`, `recover` или `restart`.
pub fn arm(plan: Option<(String, u64)>) {
    STATE.with(|s| {
        let mut s = s.borrow_mut();
        s.plan = plan;
        s.passes.clear();
    });
}

/// Пройти точку. `true` — на этом проходе назначен сбой.
pub fn hit(name: &str) -> bool {
    STATE.with(|s| {
        let mut s = s.borrow_mut();
        let n = {
            let c = s.passes.entry(name.to_string()).or_insert(0);
            *c += 1;
            *c
        };
        matches!(&s.plan, Some((p, k)) if p == name && *k == n)
    })
}

/// Обычная точка: на назначенном проходе — немедленный выход.
pub fn point(name: &str) {
    if hit(name) {
        crash();
    }
}

pub fn crash() -> ! {
    std::process::exit(CRASH_EXIT)
}
