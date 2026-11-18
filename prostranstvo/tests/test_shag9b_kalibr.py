"""Шаг 9б, стенд калибровки (спецификация шага 9б, §17): нагрузки
(phone9/assetsK9.py) и разбор энергии (phone9/razborK9.py).

Программы «наибольшие» и «малые» задают один вопрос, пока у контура есть
бюджет; ответ модели ложится на место вопроса (контракт §17), поэтому
каждый следующий вызов обязан уйти с теми же байтами. Вызов ворот здесь
изображается: функция call пишет A байт ответа с адреса вопроса, как сделал
бы исполнитель, а на n-м вызове кончает контур, как пустой бюджет.

Разбор энергии проверяется на придуманном журнале батареи, где ответ
известен заранее: единица тока по величине, знак разряда, интеграл по
отрезку за вычетом фона, сверка с убылью заряда.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import wasmtime

from experiments.measure9b import WRITE_Q

HERE = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


K = _load("assetsK9", HERE / "phone9" / "assetsK9.py")
R = _load("razborK9", HERE / "phone9" / "razborK9.py")


class Budget(Exception):
    pass


def asked(wasm: bytes, n: int) -> list[bytes]:
    engine = wasmtime.Engine()
    store = wasmtime.Store(engine)
    module = wasmtime.Module(engine, wasm)
    seen, mem = [], {}

    def call(cap: int, op: int, a: int, b: int) -> int:
        if len(seen) == n:
            raise Budget()
        assert (cap, op) == (K.CAP_MODEL, K.OP_CALL)
        m = mem["m"]
        seen.append(bytes(m.read(store, a, a + b)))
        m.write(store, b"9" * K.A, a)                    # ответ полной длины на место вопроса
        return K.A

    i_call = wasmtime.Func(store, wasmtime.FuncType([wasmtime.ValType.i32()] * 4, [wasmtime.ValType.i32()]), call)
    i_spawn = wasmtime.Func(store, wasmtime.FuncType([], [wasmtime.ValType.i32()]), lambda: -1)
    inst = wasmtime.Instance(store, module, [i_call, i_spawn])
    mem["m"] = inst.exports(store)["memory"]
    with pytest.raises(Exception):
        inst.exports(store)["main"](store)
    return seen


@pytest.mark.parametrize("b", K.SIZES)
def test_dense_question_is_exactly_b_bytes(b):
    q = K.dense(b)
    assert len(q) == b and q.endswith(b"\n" + WRITE_Q)
    assert q[: b - len(WRITE_Q) - 1].isdigit()


@pytest.mark.parametrize("name, b", [("big.wasm", 4096), ("small.wasm", 256)])
def test_askers_repeat_same_bytes_despite_answers(name, b):
    qs = K.questions()
    seen = asked(K.asker(qs[f"Р{b}"]), 7)
    assert seen == [qs[f"Р{b}"]] * 7


def test_scenarios_are_mode_2_with_owner_A():
    sc = K.scenarios()
    assert sc["cfg"]["model"] == 2 and sc["cfg"]["model_answer"] == 256 and sc["cfg"]["budget"] == 625
    assert sc["cfg"]["model_byte_unit"] == 906
    assert list(sc["words"]) == list(K.LIVE)


def test_assets_on_disk_are_current():
    """В APK идут файлы из phone9/assets/k9 — они обязаны совпадать с тем, что строит assetsK9."""
    d = HERE / "phone9" / "assets" / "k9"
    on_disk = json.loads((d / "questions.json").read_text(encoding="utf-8"))
    import base64
    assert {k: base64.b64decode(v) for k, v in on_disk.items()} == K.questions()
    assert (d / "big.wasm").read_bytes() == K.asker(K.questions()["Р4096"])
    assert (d / "small.wasm").read_bytes() == K.asker(K.questions()["Р256"])
    assert json.loads((d / "scenarios.json").read_text(encoding="utf-8")) == K.scenarios()


# ---------------------------------------------------------------- разбор энергии

def rows(i_raw: float, v_mv: float, ms: list[int], level: list[int] | None = None, plugged=False) -> list[dict]:
    lv = level or [80] * len(ms)
    return [{"ms": float(t), "i_raw": i_raw, "v_mv": v_mv, "level": float(l), "q_uah": None, "temp_c": 30.0,
             "plugged": 1.0 if plugged else 0.0} for t, l in zip(ms, lv)]


def test_microamps_and_discharge_sign():
    pts, u = R.power(rows(-500000, 4000, list(range(0, 10001, 500))))
    assert u["единица тока"] == "мкА" and u["знак разряда"] == -1
    assert all(abs(p - 2.0) < 1e-9 for _, p in pts)


def test_milliamps():
    pts, u = R.power(rows(500, 4000, list(range(0, 10001, 500))))
    assert u["единица тока"] == "мА" and all(abs(p - 2.0) < 1e-9 for _, p in pts)


def test_integral_minus_base():
    pts = [(float(t), 6.0) for t in range(0, 10001, 500)]
    assert abs(R.integral(pts, 0, 10000, base=2.0) - 40.0) < 1e-9      # (6 − 2) Вт × 10 с
    assert R.integral(pts, 20000, 30000) is None


def test_plugged_rows_are_not_energy():
    pts, u = R.power(rows(-500000, 4000, list(range(0, 10001, 500)), plugged=True))
    assert pts == [] and "ток" in u


def _hour(power_w: float, drop_pct: int) -> list[dict]:
    ms = list(range(0, 3_600_001, 10_000))
    i = -power_w / 4.0 * 1e6                                              # мкА при 4 В
    lv = [80 - round(drop_pct * k / (len(ms) - 1)) for k in range(len(ms))]
    return rows(i, 4000, ms, lv)


def test_unit_check_agrees():
    r = _hour(R.WH_NOMINAL * 0.20, 20)                                    # за час ушло 20 % ёмкости
    pts, _ = R.power(r)
    assert R.unit_check(r, pts)["единицы верны"] == "да"


def test_unit_check_catches_half():
    r = _hour(R.WH_NOMINAL * 0.10, 20)                                    # датчик занижает вдвое
    pts, _ = R.power(r)
    assert R.unit_check(r, pts)["единицы верны"].startswith("нет")


def test_unit_check_needs_drop():
    r = _hour(R.WH_NOMINAL * 0.05, 5)
    pts, _ = R.power(r)
    assert R.unit_check(r, pts)["единицы верны"].startswith("мало данных")


def test_unit_check_breaks_at_log_pause():
    # Стенд остановлен на 51 %, телефон заряжен, стенд снова пущен со 100 %:
    # убыль — 49 + 20, а не 100 − 80, и паузу не интегрировать.
    a = _hour(R.WH_NOMINAL * 0.49, 49)
    for r, lv in zip(a, [100 - round(49 * k / (len(a) - 1)) for k in range(len(a))]):
        r["level"] = float(lv)
    b = _hour(R.WH_NOMINAL * 0.20, 20)
    for r, lv in zip(b, [100 - round(20 * k / (len(b) - 1)) for k in range(len(b))]):
        r["level"] = float(lv)
        r["ms"] += 3_600_000 + 2_400_000                                   # 40 минут паузы
    pts, _ = R.power(a + b)
    u = R.unit_check(a + b, pts)
    assert [x["убыль, %"] for x in u["отрезки"]] == [49.0, 20.0] and u["единицы верны"] == "да"


def test_banki_double_the_power():
    r = _hour(R.WH_NOMINAL * 0.10, 20)                                    # датчик — одна банка из двух
    pts, u = R.power(r, banki=2)
    assert u["множитель мощности (банок)"] == 2 and R.unit_check(r, pts)["единицы верны"] == "да"


def test_unfinished_k6_scenario_is_marked_not_checked(tmp_path):
    # Сценарий К6, прерванный посередине: снимки есть, итога запуска нет.
    (tmp_path / "k6_chestny" / "snap00").mkdir(parents=True)
    (tmp_path / "k6_chestny" / "snap00" / "journal5.jsonl").write_text("", encoding="utf-8")
    out = R.laws_k6(tmp_path)
    assert out["k6_chestny"]["законов проверено"] == 0 and not out["k6_chestny"]["нарушено"]
    assert "не закончен" in out["k6_chestny"]["итог"]


def test_every_asset_the_stands_ask_for_is_packed():
    """Имена нагрузок в коде стендов — пути внутри phone9/assets, одни и те же
    на телефоне и на сервере (Device9). Каждое обязано лежать там: иначе
    стенд упадёт на телефоне, хотя на сервере прошёл."""
    import re
    src = HERE / "phone9" / "src"
    names = {m for p in src.rglob("*.java") for m in re.findall(r'dev\.asset\("([^"]+)"\)', p.read_text(encoding="utf-8"))}
    assert names, "в коде стендов не найдено ни одной нагрузки"
    missing = sorted(n for n in names if not (HERE / "phone9" / "assets" / n).is_file())
    assert not missing, missing


def test_phone_does_not_rename_assets():
    """Телефон открывает нагрузку по тому же пути, что и сервер (Device9):
    стенд калибровки однажды упал на телефоне, потому что AndroidDevice9
    приписывал к имени «w9/», а сервер — нет."""
    src = (HERE / "phone9" / "src" / "stend" / "android" / "AndroidDevice9.java").read_text(encoding="utf-8")
    assert "getAssets().open(name)" in src
