"""Второй, независимый вычислитель неподвижной точки шага 4.

Не пользуется таблицей правил из core/mir4.py: те же правила (спецификация
шага 4, §3.4 и §5) написаны здесь заново прямыми циклами по множествам. Из
core/mir4.py берутся только исходные факты — это описание мира, а не
правила. Совпадение неподвижных точек двух вычислителей для всех вариантов
— довод, что «наружу пути нет» не артефакт одного способа вычислять.
"""

from __future__ import annotations

from core.mir4 import OUTSIDE, initial_facts


def fixpoint_direct(variant: str | None) -> set[tuple]:
    F = set(initial_facts(variant))
    while True:
        cap = {(x[1], x[2], x[3]) for x in F if x[0] == "право"}
        info = {(x[1], x[2]) for x in F if x[0] == "сведения"}
        refs = {(x[1], x[2], x[3]) for x in F if x[0] == "ссылка"}
        mem = {x[1]: x[2] for x in F if x[0] == "память"}
        alive = {x[1] for x in F if x[0] == "есть"}
        parent = {x[1]: x[2] for x in F if x[0] == "родитель"}
        inputs = {x[1]: x[2] for x in F if x[0] == "вход-человека"}
        objs = {x[1] for x in F if x[0] == "объект"}
        new: set[tuple] = set()

        for (s, o, r) in cap:
            if r == "чт":
                for (o2, lbl) in info:
                    if o2 == o:
                        new.add(("сведения", mem[s], lbl))
                for (o3, o4, r4) in refs:
                    if o3 == o:
                        new.add(("право", s, o4, r4))
            if r == "зап":
                for (o2, lbl) in info:
                    if o2 == mem[s]:
                        new.add(("сведения", o, lbl))
                for (s2, o4, r4) in cap:
                    if s2 == s:
                        new.add(("ссылка", o, o4, r4))
            if o == "резольвер" and r == "выз":
                new.add(("вызов", s, "резольвер"))
                if variant == "Д2":
                    for (o2, lbl) in info:
                        if o2 == mem[s]:
                            new.add(("сведения", OUTSIDE, lbl))
                        if o2 == OUTSIDE:
                            new.add(("сведения", mem[s], lbl))
            if variant == "Д6" and o == "очередь" and r == "выз":
                for (o2, lbl) in info:
                    if o2 == mem[s]:
                        new.add(("сведения", "очередь", lbl))
                    if o2 == "очередь":
                        new.add(("сведения", mem[s], lbl))

        for h, a in parent.items():
            if a in alive:
                new.add(("есть", h))
            if h in alive:
                for (s, o, r) in cap:
                    if s == a:
                        new.add(("право", h, o, r))
                if variant == "Д5":
                    new.add(("право", h, OUTSIDE, "зап"))

        for i, a in inputs.items():
            new.add(("сведения", i, "Ч"))
            if variant == "Д3":
                new.add(("право", a, OUTSIDE, "зап"))

        if variant == "Д4":
            for s in alive:
                for o in objs:
                    for r in ("чт", "зап", "выз"):
                        new.add(("право", s, o, r))

        if variant == "Д7":
            for s in alive:
                for (o2, lbl) in info:
                    if o2 in objs:
                        new.add(("сведения", mem[s], lbl))

        if variant == "ретранслятор":
            relays = {(x[1], x[2]) for x in F if x[0] == "ретранслятор"}
            for (out, inp) in relays:
                for (o2, lbl) in info:
                    if o2 == out:
                        new.add(("сведения", inp, lbl))

        if new <= F:
            return F
        F |= new
