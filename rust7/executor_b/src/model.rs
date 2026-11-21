//! Процессы модели (контракты §17, §18): по одному на контур, без сети.
//!
//! Программу процесса даёт запуск (переменная `MODEL_SERVE`) — это исполняемый
//! файл, исполнитель запускает его сам, с аргументами режима. Отсутствие сети —
//! свойство ОС, а не обещание процесса; способ его обеспечить задаёт `Net`:
//! `unshare -rn ПРОГРАММА РЕЖИМ...` или проба сети и прямой запуск (§18.1).
//!
//! Процесс принадлежит исполнителю, а не запуску: команды `new`, `recover`,
//! `restart` его не перезапускают. Умерший или замолчавший процесс остаётся
//! мёртвым до конца жизни исполнителя, и его контур получает −7 (§17, шаг 10).
//! Модуль не знает ни журнала, ни бюджета: он передаёт байты туда и обратно.

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Value, json};
use std::cell::RefCell;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{Receiver, SyncSender, sync_channel};
use std::time::Duration;

const MAX_FRAME: usize = 16 * 1024;

/// One model process and its bounded request/reply queues.
struct Proc {
    child: Child,
    requests: SyncSender<String>,
    answers: Receiver<Vec<u8>>,
    alive: bool,
}

struct Pool {
    procs: [Proc; 2],
    timeout: Duration,
}

thread_local! {
    static POOL: RefCell<Option<Pool>> = const { RefCell::new(None) };
}

/// Способ обеспечить «без сети» (§18.1).
#[derive(Clone, Copy, PartialEq)]
pub enum Net {
    Netns,
    Probe,
}

unsafe extern "C" {
    fn socket(domain: i32, ty: i32, protocol: i32) -> i32;
    fn close(fd: i32) -> i32;
    fn prctl(option: i32, arg2: u64, arg3: u64, arg4: u64, arg5: u64) -> i32;
    fn getppid() -> i32;
}

/// Проба сети (§18.1): четыре сокета (IPv4/IPv6 × поток/датаграмма) функцией
/// `socket()` библиотеки C, а не системным вызовом — подменная `socket()` судьи
/// видит только её. Годится лишь отказ «нет права» (EACCES, EPERM) у всех
/// четырёх. Созданный сокет закрывается; любая иная ошибка пробе не понятна.
/// Не видит местных каналов между процессами устройства.
fn probe_no_network() -> Result<(), String> {
    const AF_INET: i32 = 2;
    const AF_INET6: i32 = 10;
    const SOCK_STREAM: i32 = 1;
    const SOCK_DGRAM: i32 = 2;
    for fam in [AF_INET, AF_INET6] {
        for ty in [SOCK_STREAM, SOCK_DGRAM] {
            // SAFETY: вызов libc без указателей; дескриптор сразу закрывается.
            let fd = unsafe { socket(fam, ty, 0) };
            if fd >= 0 {
                unsafe { close(fd) };
                return Err(format!("проба сети: сокет (семейство {fam}, вид {ty}) создался — сеть не отнята"));
            }
            let err = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
            if err != 13 && err != 1 {
                return Err(format!("проба сети: семейство {fam}, вид {ty}: ошибка {err}, а нужен отказ «нет права»"));
            }
        }
    }
    Ok(())
}

/// Просит систему убить этот (дочерний) процесс, когда исполнитель умрёт, как
/// бы он ни умер: конец, сбой по инъекции, убийство (§18.1). Вызывается до
/// запуска программы модели; настройка переживает `exec` и `unshare -rn`, так
/// что действует при обоих способах. Сигнал привязан к потоку, породившему
/// процесс: запуск идёт из главного потока, который живёт до конца
/// исполнителя. Если родитель умер раньше, чем просьба принята, сигнала уже не
/// будет — это видно по смене родителя, и процесс завершается сам.
fn die_with_parent(parent: i32) -> std::io::Result<()> {
    const PR_SET_PDEATHSIG: i32 = 1;
    const SIGKILL: u64 = 9;
    // SAFETY: вызовы libc без указателей.
    unsafe {
        if prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0 {
            return Err(std::io::Error::last_os_error());
        }
        if getppid() != parent {
            return Err(std::io::Error::other("исполнитель уже умер"));
        }
    }
    Ok(())
}

/// Подключена ли модель при запуске исполнителя.
pub fn connected() -> bool {
    POOL.with(|p| p.borrow().is_some())
}

/// Запустить два процесса модели. `mode` — режим одной строкой («--stub»,
/// «--script ФАЙЛ»), он делится по пробелам, поэтому пути с пробелами не
/// поддержаны. `Err` — причина, по которой исполнитель не может стартовать.
pub fn start(mode: &str, timeout_s: f64, net: Net) -> Result<(), String> {
    let serve = std::env::var("MODEL_SERVE").map_err(|_| "--model задан, а переменной MODEL_SERVE нет".to_string())?;
    if !std::path::Path::new(&serve).is_file() {
        return Err(format!("MODEL_SERVE: файла нет ({serve})"));
    }
    if !timeout_s.is_finite() || timeout_s <= 0.0 {
        return Err("--model-timeout: положительное число секунд".into());
    }
    // Проверка заранее: иначе неработающий `unshare` или сеть, которая есть,
    // выглядели бы как «модель молчит», и первый же вопрос стоил бы бюджета.
    match net {
        Net::Netns => {
            let probe = Command::new("unshare").args(["-rn", "true"]).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).status();
            if !matches!(probe, Ok(s) if s.success()) {
                return Err("unshare -rn не работает: модель без сети запустить нельзя".into());
            }
        }
        Net::Probe => probe_no_network()?,
    }
    let spawn_one = || -> Result<Proc, String> {
        let mut cmd = match net {
            Net::Netns => {
                let mut c = Command::new("unshare");
                c.args(["-rn", &serve]);
                c
            }
            Net::Probe => Command::new(&serve),
        };
        let parent = std::process::id() as i32;
        // SAFETY: в дочернем процессе между fork и exec вызываются только
        // `prctl` и `getppid` — обе допустимы там (async-signal-safe).
        unsafe { cmd.pre_exec(move || die_with_parent(parent)) };
        let mut child = cmd
            .args(mode.split_whitespace())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("процесс модели не запустился: {e}"))?;
        let mut stdin = child.stdin.take().ok_or("нет stdin процесса модели")?;
        let stdout = child.stdout.take().ok_or("нет stdout процесса модели")?;
        let (requests, incoming) = sync_channel::<String>(1);
        let (outgoing, answers) = sync_channel(1);
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            while let Ok(request) = incoming.recv() {
                if writeln!(stdin, "{request}").and_then(|_| stdin.flush()).is_err() {
                    break;
                }
                let Some(line) = read_frame(&mut reader) else { break };
                if outgoing.send(line).is_err() {
                    break;
                }
            }
        });
        Ok(Proc { child, requests, answers, alive: true })
    };
    let mut first = spawn_one()?;
    let second = spawn_one().inspect_err(|_| kill(&mut first))?;
    let procs = [first, second];
    POOL.with(|p| *p.borrow_mut() = Some(Pool { procs, timeout: Duration::from_secs_f64(timeout_s) }));
    Ok(())
}

fn read_frame(reader: &mut impl BufRead) -> Option<Vec<u8>> {
    let mut frame = Vec::new();
    reader.take((MAX_FRAME + 1) as u64).read_until(b'\n', &mut frame).ok()?;
    (frame.len() <= MAX_FRAME && frame.last() == Some(&b'\n')).then_some(frame)
}

#[cfg(test)]
mod tests {
    use super::{MAX_FRAME, read_frame};

    #[test]
    fn frame_requires_newline_within_limit() {
        let valid = vec![b'x'; MAX_FRAME - 1].into_iter().chain([b'\n']).collect::<Vec<_>>();
        assert_eq!(read_frame(&mut valid.as_slice()), Some(valid));
        assert!(read_frame(&mut &b"unfinished"[..]).is_none());
        let oversized = vec![b'x'; MAX_FRAME].into_iter().chain([b'\n']).collect::<Vec<_>>();
        assert!(read_frame(&mut oversized.as_slice()).is_none());
    }
}

/// Процесс контура `ci` не убит исполнителем. О смерти процесса самого по себе
/// исполнитель узнаёт только вопросом (§17, уточнение шага 9б): иначе журнал
/// зависел бы от того, успел ли процесс умереть. Убитым он остаётся навсегда.
pub fn alive(ci: usize) -> bool {
    POOL.with(|p| {
        let mut p = p.borrow_mut();
        let Some(pool) = p.as_mut() else { return false };
        pool.procs[ci].alive
    })
}

/// Убить процесс и больше не использовать.
fn kill(pr: &mut Proc) {
    pr.alive = false;
    let _ = pr.child.kill();
    let _ = pr.child.wait();
}

/// Спросить модель контура `ci`. `None` — ответа нет: срок вышел, процесс
/// умер или ответил не по протоколу; процесс в этом случае убит. Ответ не
/// обрезается: длину ограничивает вызывающий.
pub fn ask(ci: usize, question: &[u8], max: usize) -> Option<Vec<u8>> {
    POOL.with(|p| {
        let mut p = p.borrow_mut();
        let pool = p.as_mut()?;
        let timeout = pool.timeout;
        let pr = &mut pool.procs[ci];
        if !pr.alive {
            return None;
        }
        let req = json!({"q": STANDARD.encode(question), "max": max}).to_string();
        let sent = pr.requests.try_send(req);
        let answer = match sent {
            Ok(()) => pr.answers.recv_timeout(timeout).ok().and_then(|line| {
                let v: Value = serde_json::from_slice(&line).ok()?;
                STANDARD.decode(v.get("a")?.as_str()?).ok()
            }),
            Err(_) => None,
        };
        if answer.is_none() {
            kill(pr);
        }
        answer
    })
}
