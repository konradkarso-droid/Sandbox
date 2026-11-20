"""Validate the fixation records of the step 5b and later Python journals."""

from collections.abc import Iterable

from env.journal import JournalBroken


def validate_fixations(records: Iterable[dict]) -> None:
    checked: dict[tuple[int, str], tuple] = {}
    debited: set[tuple[int, str]] = set()
    closed: set[tuple[int, str]] = set()
    kinds = {"проверка", "списание", "отказ", "завершено", "потеряно"}

    for rec in records:
        kind = rec.get("kind")
        if kind not in kinds:
            continue
        contour, fix = rec.get("contour"), rec.get("fix")
        if (type(contour) is not int or contour not in (1, 2) or not isinstance(fix, str)
                or not fix.startswith(f"{contour}-") or not fix[2:].isascii()
                or not fix[2:].isdigit() or (len(fix) > 3 and fix[2] == "0")):
            raise JournalBroken(f"неверный номер фиксации: {fix!r}")
        key = (contour, fix)
        if key in closed:
            raise JournalBroken(f"{fix}: запись «{kind}» после закрытия фиксации")
        task = (rec.get("task"), rec.get("part"), rec.get("subject"))
        before = checked.get(key)

        if kind == "проверка":
            if before is not None or key in debited:
                raise JournalBroken(f"{fix}: повторная или поздняя запись проверки")
            checked[key] = task
        elif kind == "списание":
            if key in debited:
                raise JournalBroken(f"{fix}: повторная запись списания")
            if "task" in rec or before is not None:
                if before != task or rec.get("receiver") != "выход":
                    raise JournalBroken(f"{fix}: списание не соответствует проверке")
            debited.add(key)
        elif kind == "отказ":
            wrong_answer = rec.get("why") == "ответ неверен"
            if key in debited or wrong_answer != (before is not None) or (before is not None and before != task):
                raise JournalBroken(f"{fix}: отказ не соответствует предыдущим записям")
            closed.add(key)
        else:
            if key not in debited:
                raise JournalBroken(f"{fix}: «{kind}» без предыдущего списания")
            closed.add(key)
