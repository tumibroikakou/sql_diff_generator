#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sql_diff.py: сравнение двух mysqldump-файлов одной и той же БД.

Генерирует SQL (INSERT/UPDATE) для синхронизации второстепенной БД
по первостепенной:
  * строка есть только в первостепенном дампе -> INSERT;
  * строка есть в обоих, значения отличаются -> UPDATE (только изменившиеся колонки);
  * строки, которые есть только во второстепенном дампе, игнорируются
    (DELETE не генерируются).

Схемы сопоставляются по именам колонок; сравнение данных по первичному ключу,
для таблиц без PK по полному кортежу значений общих колонок.

Используется только стандартная библиотека Python 3.8+.
"""

import argparse
import os
import re
import sys
from collections import Counter
from datetime import datetime

# Регулярные выражения

CREATE_TABLE_RE = re.compile(r"^CREATE TABLE `([^`]+)`")
INSERT_RE = re.compile(r"^INSERT INTO `([^`]+)`\s*(\([^)]*\))?\s*VALUES\b", re.IGNORECASE)
PRIMARY_KEY_RE = re.compile(r"PRIMARY KEY\s*\(([^)]*)\)", re.IGNORECASE)
INT_RE = re.compile(r"^[-+]?\d+$")
FLOAT_RE = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


# Лексический разбор стейтментов

def skip_string(s, i):
    """Пропускает строковый литерал в одинарных кавычках, начиная с s[i] == "'".

    Учитывает backslash-экранирование (\\', \\\\, \\n, \\r, \\0, \\t, \\Z, \\x..)
    и удвоение кавычки (''). Возвращает индекс ПОСЛЕ закрывающей кавычки
    или -1, если строка не закрыта.
    """
    n = len(s)
    j = i + 1
    while j < n:
        j = s.find("'", j)
        if j == -1:
            return -1
        # считаем количество backslash'ов непосредственно перед кавычкой
        k = j - 1
        nb = 0
        while k >= 0 and s[k] == "\\":
            nb += 1
            k -= 1
        if nb % 2 == 1:  # экранированная кавычка \'
            j += 1
            continue
        if s[j + 1 : j + 2] == "'":  # удвоенная кавычка ''
            j += 2
            continue
        return j + 1
    return -1


def find_stmt_end_line(line, in_string):
    """Просматривает ОДНУ строку дампа, продолжая состояние литерала.

    in_string: True, если мы внутри строкового литерала, начатого в
    предыдущих строках. Возвращает (индекс ';' вне литерала или -1,
    состояние литерала в конце строки).

    Построчный скан даёт O(n) на весь файл. Переводы строк внутри литералов
    в mysqldump экранированы (\n), поэтому между строками переносится только
    флаг in_string.
    """
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_string:
            if c == "\\":
                i += 2  # экранированный символ: пропускаем оба
                continue
            if c == "'":
                if line[i + 1 : i + 2] == "'":  # удвоенная кавычка ''
                    i += 2
                    continue
                in_string = False
        elif c == ";":
            return i, False
        elif c == "'":
            in_string = True
        i += 1
    return -1, in_string


def parse_value(s, i):
    """Разбирает одно значение кортежа с позиции i.

    Возвращает (сырое_значение_как_в_дампе, новая_позиция).
    Строковые литералы возвращаются КАК ЕСТЬ, с сохранением экранирования,
    что позволяет генерировать выходной SQL без повторного экранирования.
    """
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    if i >= n:
        raise ValueError("неожиданный конец данных при разборе значения")
    c = s[i]
    if c == "'":
        j = skip_string(s, i)
        if j == -1:
            raise ValueError("незакрытый строковый литерал")
        return s[i:j], j
    # нестроковое значение: число, NULL, hex-литерал 0x...
    j = i
    while j < n and s[j] not in ",)":
        j += 1
    return s[i:j].strip(), j


def parse_row(s, i):
    """Разбирает один кортеж значений; s[i] должен быть '('.

    Возвращает (список_сырых_значений, позиция_после_закрывающей_скобки).
    """
    n = len(s)
    i += 1
    vals = []
    while True:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n:
            raise ValueError("незакрытый кортеж значений")
        c = s[i]
        if c == ")":
            return vals, i + 1
        if c == ",":
            i += 1
            continue
        v, i = parse_value(s, i)
        vals.append(v)


def parse_tuples(s, memo):
    """Разбирает текст после VALUES: '(..),(..),...'.

    Возвращает список кортежей сырых значений. Корректно обрабатывает
    вложенные скобки внутри строковых литералов, NULL, числа, hex (0x...).
    """
    rows = []
    n = len(s)
    i = 0
    while i < n:
        c = s[i]
        if c in " \t\r\n,":
            i += 1
            continue
        if c == ";":
            break
        if c != "(":
            raise ValueError("ожидается '(' на позиции %d: %r" % (i, s[i : i + 30]))
        row, i = parse_row(s, i)
        # интернирование повторяющихся значений экономит память
        rows.append(tuple(memo.setdefault(v, v) for v in row))
    return rows


# Разбор схемы (CREATE TABLE)

def parse_create_table(name, text):
    """Извлекает список колонок (в порядке следования) и PRIMARY KEY.

    Возвращает (cols, pk), где pk: список имён колонок (пустой, если PK нет).
    """
    cols = []
    pk = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^`([^`]+)`", line)
        if m:
            cols.append(m.group(1))
    m = PRIMARY_KEY_RE.search(text)
    if m:
        pk = [x.strip().strip("`") for x in m.group(1).split(",") if x.strip()]
    return cols, pk


# строка тела CREATE TABLE: определение колонки, индекс или ограничение
COLDEF_RE = re.compile(r"^`([^`]+)`\s+(.+)$")
INDEX_KIND_RE = re.compile(
    r"^(PRIMARY\s+KEY|UNIQUE\s+(?:KEY|INDEX)|(?:FULLTEXT|SPATIAL)\s+(?:KEY|INDEX)"
    r"|FOREIGN\s+KEY|CONSTRAINT|KEY|INDEX)\b",
    re.IGNORECASE,
)
FK_COLS_RE = re.compile(r"FOREIGN\s+KEY\s*\(([^)]*)\)", re.IGNORECASE)


def parse_create_body(text):
    """Разбирает тело CREATE TABLE построчно.

    Возвращает (col_defs, keys, fks):
      col_defs: имя колонки -> определение после имени (тип, NOT NULL, DEFAULT,
                COMMENT, AUTO_INCREMENT и т.д., без завершающей запятой);
      keys:     список (kind, kname, kline, kcols), kind: "PRIMARY"/"UNIQUE"/"KEY",
                kname: имя индекса или None, kline: исходная строка определения,
                kcols: множество колонок индекса;
      fks:      список (fkline, fkcols): определения FOREIGN KEY (с CONSTRAINT
                или без) и множество колонок внешнего ключа.
    """
    col_defs = {}
    keys = []
    fks = []
    for raw in text.splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        if line.startswith(")") or line.startswith("CREATE") or line.startswith("DROP"):
            continue
        m = COLDEF_RE.match(line)
        if m:
            col_defs[m.group(1)] = m.group(2).strip()
            continue
        m = INDEX_KIND_RE.match(line)
        if not m:
            continue
        kind_upper = m.group(1).upper()
        names = re.findall(r"`([^`]+)`", line)
        if kind_upper.startswith("PRIMARY"):
            keys.append(("PRIMARY", None, line, set(names)))
        elif kind_upper.startswith("FOREIGN") or kind_upper == "CONSTRAINT":
            fm = FK_COLS_RE.search(line)
            fkcols = set()
            if fm:
                fkcols = {x.strip().strip("`") for x in fm.group(1).split(",") if x.strip()}
            fks.append((line, fkcols))
        elif kind_upper.startswith("UNIQUE"):
            kname = names[0] if names else None
            keys.append(("UNIQUE", kname, line, set(names[1:])))
        else:  # KEY / INDEX / FULLTEXT / SPATIAL
            kname = names[0] if names else None
            keys.append(("KEY", kname, line, set(names[1:])))
    return col_defs, keys, fks


def gen_table_alters(name, missing_cols, p_create, s_create):
    """Генерирует ALTER-стейтменты для колонок, отсутствующих во второстепенной схеме.

    Возвращает (alters, notes): alters: список SQL-команд (ADD COLUMN, при
    необходимости ADD PRIMARY KEY / ADD [UNIQUE] KEY / ADD CONSTRAINT ... FOREIGN KEY),
    notes: текстовые предупреждения.
    """
    p_col_defs, p_keys, p_fks = parse_create_body(p_create)
    s_has_pk = PRIMARY_KEY_RE.search(s_create) is not None
    p_pk_cols = set()
    for kind, _kname, kline, kcols in p_keys:
        if kind == "PRIMARY":
            p_pk_cols = kcols
            p_pk_line = kline
    alters = []
    seen = set()
    notes = []
    for col in missing_cols:
        definition = p_col_defs.get(col)
        if definition is None:
            notes.append(
                "[%s] не удалось извлечь определение колонки `%s` из CREATE TABLE"
                % (name, col)
            )
            continue
        stmt = "ALTER TABLE `%s` ADD COLUMN `%s` %s;" % (name, col, definition)
        if stmt not in seen:
            seen.add(stmt)
            alters.append(stmt)
        if col in p_pk_cols:
            if s_has_pk:
                notes.append(
                    "[%s] колонка `%s` входит в PRIMARY KEY, но PRIMARY KEY уже есть "
                    "во второстепенной таблице, ADD PRIMARY KEY не сгенерирован"
                    % (name, col)
                )
            else:
                stmt = "ALTER TABLE `%s` ADD %s;" % (name, p_pk_line)
                if stmt not in seen:
                    seen.add(stmt)
                    alters.append(stmt)
        for kind, kname, kline, kcols in p_keys:
            if kind != "PRIMARY" and col in kcols:
                if kname and "`%s`" % kname in s_create:
                    continue  # индекс с таким именем уже есть во второстепенном
                stmt = "ALTER TABLE `%s` ADD %s;" % (name, kline)
                if stmt not in seen:
                    seen.add(stmt)
                    alters.append(stmt)
        for fkline, fkcols in p_fks:
            if col in fkcols:
                stmt = "ALTER TABLE `%s` ADD %s;" % (name, fkline)
                if stmt not in seen:
                    seen.add(stmt)
                    alters.append(stmt)
    return alters, notes


# Разбор файла дампа

def parse_dump(path, want_data):
    """Разбирает mysqldump-файл.

    want_data(имя_таблицы) -> bool: собирать ли данные (rows) этой таблицы.
    Схема (CREATE TABLE) собирается всегда.

    Возвращает (schemas, data):
      schemas: имя -> (cols, pk)
      data:    имя -> список кортежей сырых значений (только для want_data)
    """
    schemas = {}
    data = {}
    memo = {}
    create_name = None
    create_buf = []
    pending_drop = None  # DROP TABLE IF EXISTS, предшествующий CREATE TABLE
    insert_lines = None    # строки текущего незавершённого многострочного INSERT
    ins_in_string = False  # состояние строкового литерала при построчном скане
    ins_base = 0           # суммарная длина строк до текущей (позиция ';' в стейтменте)
    warnings = []

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # --- продолжение незавершённого INSERT ---
            if insert_lines is not None:
                end, ins_in_string = find_stmt_end_line(line, ins_in_string)
                if end == -1:
                    insert_lines.append(line)
                    ins_base += len(line)
                    continue
                insert_lines.append(line)
                stmt = "".join(insert_lines)[: ins_base + end]
                insert_lines = None
                _handle_insert(stmt, schemas, data, memo, want_data, warnings, path)
                continue

            stripped = line.strip()

            # --- накопление CREATE TABLE ---
            if create_name is not None:
                create_buf.append(line)
                if stripped.startswith(")"):
                    text = "".join(create_buf)
                    if not text.rstrip().endswith(";"):
                        text = text.rstrip() + ";\n"
                    cols, pk = parse_create_table(create_name, text)
                    # третий элемент: полный текст стейтмента с предшествующим
                    # DROP TABLE IF EXISTS, нужен для генерации DDL
                    schemas[create_name] = (cols, pk, text)
                    create_name = None
                continue

            if not stripped or stripped.startswith("--") or stripped.startswith("/*"):
                continue

            if stripped.startswith("DROP TABLE"):
                pending_drop = stripped
                continue

            if stripped.startswith("CREATE TABLE"):
                m = CREATE_TABLE_RE.match(stripped)
                if m:
                    create_name = m.group(1)
                    create_buf = ([pending_drop + "\n"] if pending_drop else []) + [line]
                    pending_drop = None
                continue

            pending_drop = None

            if stripped.startswith("INSERT"):
                end, st = find_stmt_end_line(line, False)
                if end == -1:
                    # многострочный INSERT: накапливаем строки
                    insert_lines = [line]
                    ins_in_string = st
                    ins_base = len(line)
                else:
                    _handle_insert(
                        line[:end], schemas, data, memo, want_data, warnings, path
                    )
            # всё остальное (SET, LOCK/UNLOCK, DROP, условные комментарии) пропускаем

    if insert_lines is not None:
        warnings.append(
            "файл %s: незавершённый INSERT-стейтмент в конце файла, данные пропущены"
            % path
        )
    return schemas, data, warnings


def _handle_insert(stmt, schemas, data, memo, want_data, warnings, path):
    m = INSERT_RE.match(stmt)
    if not m:
        warnings.append(
            "файл %s: не удалось разобрать INSERT-стейтмент: %r..."
            % (path, stmt[:60])
        )
        return
    name = m.group(1)
    if not want_data(name):
        return
    rows = parse_tuples(stmt[m.end() :], memo)
    insert_cols = None
    if m.group(2):
        insert_cols = [x.strip().strip("`") for x in m.group(2)[1:-1].split(",")]
    # если в INSERT указан список колонок, приводим строки к порядку колонок схемы
    if insert_cols is not None and name in schemas:
        schema_cols = schemas[name][0]
        col_pos = {c: i for i, c in enumerate(insert_cols)}
        reordered = []
        for row in rows:
            new = ["NULL"] * len(schema_cols)
            for i, c in enumerate(schema_cols):
                j = col_pos.get(c)
                if j is not None and j < len(row):
                    new[i] = row[j]
            reordered.append(tuple(new))
        rows = reordered
    data.setdefault(name, []).extend(rows)


# Сравнение значений

# escape-последовательности переводов строк внутри литералов: обычная форма
# (\r\n, \n, \r) и удвоенная (\\r\\n и т.п., экранированный backslash);
# более длинные варианты идут первыми
NL_ESCAPES_RE = re.compile(r"\\\\r\\\\n|\\\\n|\\\\r|\\r\\n|\\n|\\r")


def _strip_nl_escapes(inner):
    """Вырезает escape-последовательности переводов строк (\\r\\n, \\n, \\r,
    включая удвоенные \\\\r\\\\n, \\\\n, \\\\r) из содержимого литерала."""
    return NL_ESCAPES_RE.sub("", inner)


def norm_string_literal(v):
    """Нормализует строковый литерал: вырезает \\r\\n, \\n, \\r (и удвоенные
    \\\\r\\\\n, \\\\n, \\\\r) внутри кавычек.

    Нестроковые значения (числа, NULL, hex) возвращаются как есть.
    """
    if v[:1] == "'" and v[-1:] == "'" and ("\\n" in v or "\\r" in v):
        return "'" + _strip_nl_escapes(v[1:-1]) + "'"
    return v


def out_value(v):
    """Значение для вывода в выходной SQL: из строковых литералов вырезаются
    escape-последовательности переводов строк (\\r\\n, \\n, \\r)."""
    return norm_string_literal(v)


def values_equal(a, b):
    """Сравнение сырых SQL-литералов с нормализацией чисел (1 == 1.0 и т.п.)
    и переводов строк (\\r\\n, \\n, \\r внутри строковых литералов игнорируются)."""
    if a == b:
        return True
    if INT_RE.match(a) and INT_RE.match(b):
        return int(a) == int(b)
    if FLOAT_RE.match(a) and FLOAT_RE.match(b):
        return float(a) == float(b)
    if a[:1] == "'" and b[:1] == "'" and ("\\n" in a or "\\r" in a or "\\n" in b or "\\r" in b):
        return norm_string_literal(a) == norm_string_literal(b)
    return False


# Сравнение таблиц

class TableDiff(object):
    __slots__ = (
        "inserts", "updates", "missing_cols", "no_pk", "dup_pk", "sec_absent",
        "alters", "common",
    )

    def __init__(self):
        self.inserts = []       # строки первостепенного дампа (кортежи сырых значений)
        self.updates = []       # (pk_значения, [(колонка, новое, старое), ...])
        self.missing_cols = []  # колонки первостепенного, которых нет во второстепенном
        self.no_pk = False
        self.dup_pk = 0
        self.sec_absent = False
        self.alters = []        # сгенерированные ALTER-стейтменты (DDL)
        self.common = []        # общие колонки с учётом добавляемых ALTER-ами


def diff_table(p_cols, p_pk, p_rows, s_cols, s_rows):
    """Сравнивает строки одной таблицы. Возвращает TableDiff."""
    d = TableDiff()
    if s_cols is None:
        # таблицы нет во второстепенном дампе: все строки идут в INSERT,
        # схема будет создана через CREATE TABLE (см. генерацию DDL)
        d.sec_absent = True
        d.inserts.extend(p_rows)
        return d
    s_set = set(s_cols)
    d.missing_cols = [c for c in p_cols if c not in s_set]

    # после генерации ALTER TABLE ADD COLUMN колонка считается существующей:
    # недостающие колонки участвуют в INSERT/UPDATE как обычно
    s_cols_eff = list(s_cols) + d.missing_cols
    # дополняем короткие строки второстепенного дампа NULL-значениями
    need = len(s_cols_eff)
    s_rows = [
        r if len(r) >= need else r + ("NULL",) * (need - len(r))
        for r in s_rows
    ]
    s_idx = {c: i for i, c in enumerate(s_cols_eff)}
    common = [c for c in p_cols if c in s_idx]
    d.common = common

    p_idx = {c: i for i, c in enumerate(p_cols)}
    c_p = [p_idx[c] for c in common]
    c_s = [s_idx[c] for c in common]

    use_pk = bool(p_pk) and all(c in s_set for c in p_pk)
    if not use_pk:
        d.no_pk = True
        # для сопоставления строк без PK используем только реально существующие
        # колонки (добавляемые ALTER-ами во второстепенном ещё NULL)
        common = [c for c in p_cols if c in s_set]
        c_p = [p_idx[c] for c in common]
        c_s = [s_idx[c] for c in common]

    if use_pk:
        pk_p = [p_idx[c] for c in p_pk]
        pk_s = [s_idx[c] for c in p_pk]
        sec = {}
        for row in s_rows:
            k = tuple(row[i] for i in pk_s)
            if k in sec:
                d.dup_pk += 1
            else:
                sec[k] = row
        for row in p_rows:
            k = tuple(row[i] for i in pk_p)
            srow = sec.get(k)
            if srow is None:
                d.inserts.append(row)
                continue
            changed = []
            for j, cname in enumerate(common):
                pv = row[c_p[j]]
                sv = srow[c_s[j]]
                if not values_equal(pv, sv):
                    changed.append((cname, pv, sv))  # (колонка, новое, старое)
            if changed:
                d.updates.append((k, changed))
    else:
        # сравнение по полному кортежу общих колонок, дубликаты не теряются
        if common:
            sec_counter = Counter(tuple(row[i] for i in c_s) for row in s_rows)
            for row in p_rows:
                k = tuple(row[i] for i in c_p)
                if sec_counter[k] > 0:
                    sec_counter[k] -= 1
                else:
                    d.inserts.append(row)
        else:
            # общих колонок нет: все строки первостепенного вставляются
            d.inserts.extend(p_rows)
    return d


# Генерация SQL

def gen_insert(name, cols, row, p_idx):
    vals = ", ".join(out_value(row[p_idx[c]]) for c in cols)
    collist = ", ".join("`%s`" % c for c in cols)
    return "INSERT INTO `%s` (%s) VALUES (%s);" % (name, collist, vals)


def gen_update(name, pk_cols, pk_vals, changed):
    # changed: список (колонка, новое_значение, старое_значение)
    set_part = ", ".join("`%s`=%s" % (c, out_value(v)) for c, v, _old in changed)
    where = " AND ".join("`%s`=%s" % (c, out_value(v)) for c, v in zip(pk_cols, pk_vals))
    return "UPDATE `%s` SET %s WHERE %s;" % (name, set_part, where)


# CLI

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Сравнение двух mysqldump-файлов и генерация SQL "
        "синхронизации второстепенной БД с первостепенной (INSERT/UPDATE, без DELETE)."
    )
    p.add_argument("primary", help="путь к первостепенному дампу (источник истины)")
    p.add_argument("secondary", help="путь ко второстепенному дампу (синхронизируемая БД)")
    p.add_argument(
        "-o", "--output", default="output/diff.sql",
        help="путь к выходному .sql файлу (по умолчанию: output/diff.sql)",
    )
    p.add_argument(
        "-e", "--exclude", default="",
        help="список таблиц через запятую, которые полностью игнорировать",
    )
    p.add_argument(
        "-t", "--tables", default="",
        help="обрабатывать только перечисленные таблицы (через запятую)",
    )
    p.add_argument(
        "--report", default="output/report.txt",
        help="путь к текстовому отчёту (по умолчанию: output/report.txt)",
    )
    p.add_argument(
        "--no-report", action="store_true",
        help="не записывать отчёт в файл (сводка печатается в stdout)",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    for path in (args.primary, args.secondary):
        if not os.path.isfile(path):
            sys.exit("Ошибка: файл не найден: %s" % path)

    exclude = {t.strip() for t in args.exclude.split(",") if t.strip()}
    only = {t.strip() for t in args.tables.split(",") if t.strip()} or None

    def want_table(t):
        if exclude and t in exclude:
            return False
        if only is not None and t not in only:
            return False
        return True

    print("Разбор первостепенного дампа: %s ..." % args.primary)
    p_schemas, p_data, warn1 = parse_dump(
        args.primary, lambda t: want_table(t)
    )
    print("  таблиц со схемой: %d" % len(p_schemas))

    print("Разбор второстепенного дампа: %s ..." % args.secondary)
    # данные второстепенного дампа нужны только для обрабатываемых таблиц,
    # присутствующих в первостепенном
    s_schemas, s_data, warn2 = parse_dump(
        args.secondary, lambda t: want_table(t) and t in p_schemas
    )
    print("  таблиц со схемой: %d" % len(s_schemas))

    warnings = list(warn1) + list(warn2)

    if only is not None:
        unknown = sorted(only - set(p_schemas) - set(s_schemas))
        if unknown:
            warnings.append(
                "таблицы из --tables, отсутствующие в обоих дампа: %s"
                % ", ".join(unknown)
            )

    # сравнение
    results = {}       # имя -> (TableDiff, p_cols, p_pk)
    total_ins = total_upd = 0
    for name in sorted(p_schemas):
        if not want_table(name):
            continue
        p_cols, p_pk, p_create = p_schemas[name]
        p_rows = p_data.get(name, [])
        if name in s_schemas:
            s_cols = s_schemas[name][0]
            s_rows = s_data.get(name, [])
        else:
            s_cols, s_rows = None, []
        d = diff_table(p_cols, p_pk, p_rows, s_cols, s_rows)
        d.sec_absent = name not in s_schemas
        results[name] = (d, p_cols, p_pk, p_create)
        total_ins += len(d.inserts)
        total_upd += len(d.updates)

    # генерация DDL для недостающих объектов схемы
    ddl_creates = 0   # CREATE TABLE для таблиц, отсутствующих во второстепенном
    ddl_alters = 0    # ALTER TABLE для недостающих колонок
    for name in sorted(results):
        d, p_cols, p_pk, p_create = results[name]
        if d.sec_absent:
            ddl_creates += 1
        elif d.missing_cols:
            d.alters, alter_notes = gen_table_alters(
                name, d.missing_cols, p_create, s_schemas[name][2]
            )
            ddl_alters += len(d.alters)
            warnings.extend(alter_notes)

    # генерация выходного SQL
    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    with open(args.output, "w", encoding="utf-8", newline="\n") as out:
        out.write("-- ============================================================\n")
        out.write("-- SQL синхронизации, сгенерирован sql_diff.py\n")
        out.write("-- Дата: %s\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        out.write("-- Первостепенный дамп: %s\n" % args.primary)
        out.write("-- Второстепенный дамп: %s\n" % args.secondary)
        out.write("-- Таблиц: %d, INSERT: %d, UPDATE: %d\n"
                  % (len(results), total_ins, total_upd))
        out.write("-- DDL: CREATE TABLE: %d, ALTER TABLE: %d\n"
                  % (ddl_creates, ddl_alters))
        out.write("-- ============================================================\n")
        out.write("SET NAMES utf8mb4;\n")
        out.write("SET FOREIGN_KEY_CHECKS=0;\n\n")

        # DDL: полные CREATE TABLE для таблиц, отсутствующих во второстепенном
        if ddl_creates:
            out.write("-- ===== DDL: создание таблиц, отсутствующих во второстепенной БД =====\n")
            for name in sorted(results):
                d = results[name][0]
                if not d.sec_absent:
                    continue
                out.write(results[name][3])
                out.write("\n")
            out.write("\n")

        for name in sorted(results):
            d, p_cols, p_pk, _p_create = results[name]
            if not d.inserts and not d.updates and not d.alters:
                continue
            out.write("-- ===== table: %s (INSERT: %d, UPDATE: %d) =====\n"
                      % (name, len(d.inserts), len(d.updates)))
            # ALTER-ы для недостающих колонок перед данными таблицы
            for stmt in d.alters:
                out.write(stmt)
                out.write("\n")
            if d.alters:
                out.write("\n")
            p_idx = {c: i for i, c in enumerate(p_cols)}
            # колонки для INSERT: общие с учётом добавляемых ALTER-ами, или все
            # колонки первостепенного, если таблицы нет во второстепенном дампе
            if d.sec_absent:
                ins_cols = list(p_cols)
            else:
                ins_cols = d.common
            for row in d.inserts:
                out.write(gen_insert(name, ins_cols, row, p_idx))
                out.write("\n")
            for pk_vals, changed in d.updates:
                out.write(gen_update(name, p_pk, pk_vals, changed))
                out.write("\n")
            out.write("\n")
        out.write("SET FOREIGN_KEY_CHECKS=1;\n")

    # отчёт
    lines = []
    lines.append("=" * 72)
    lines.append("ОТЧЁТ сравнения дампов (sql_diff.py)")
    lines.append("Дата: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("Первостепенный дамп: %s" % args.primary)
    lines.append("Второстепенный дамп: %s" % args.secondary)
    lines.append("=" * 72)
    lines.append("")
    lines.append("%-40s %10s %10s" % ("Таблица", "INSERT", "UPDATE"))
    lines.append("-" * 72)
    for name in sorted(results):
        d = results[name][0]
        lines.append("%-40s %10d %10d" % (name, len(d.inserts), len(d.updates)))
    lines.append("-" * 72)
    lines.append("Итого: таблиц: %d, INSERT: %d, UPDATE: %d" % (len(results), total_ins, total_upd))
    lines.append("DDL: CREATE TABLE: %d, ALTER TABLE: %d" % (ddl_creates, ddl_alters))
    lines.append("")

    notes = []
    for name in sorted(results):
        d, p_cols, p_pk, _p_create = results[name]
        if d.sec_absent:
            notes.append(
                "[%s] таблица отсутствует во второстепенном дампе: сгенерирован DDL "
                "(CREATE TABLE), все %d строк(-и) будут вставлены (INSERT)"
                % (name, len(d.inserts))
            )
        if d.missing_cols:
            notes.append(
                "[%s] колонки первостепенного дампа, отсутствующие во второстепенном "
                "(сгенерирован DDL: ALTER TABLE ADD COLUMN, колонки включены "
                "в INSERT/UPDATE): %s"
                % (name, ", ".join(d.missing_cols))
            )
        if d.no_pk:
            notes.append(
                "[%s] первичный ключ не определён или его колонки отсутствуют "
                "во второстепенном дампе, сравнение по полному кортежу общих колонок"
                % name
            )
        if d.dup_pk:
            notes.append(
                "[%s] дубликатов первичного ключа во второстепенном дампе: %d "
                "(использовано первое вхождение)" % (name, d.dup_pk)
            )

    absent_in_primary = sorted(set(s_schemas) - set(p_schemas))
    if absent_in_primary:
        notes.append(
            "таблицы второстепенного дампа, отсутствующие в первостепенном "
            "(изменения не генерируются): %s" % ", ".join(absent_in_primary)
        )
    if exclude:
        notes.append("исключённые таблицы (-e): %s" % ", ".join(sorted(exclude)))
    notes.extend(warnings)

    if notes:
        lines.append("Предупреждения и примечания:")
        lines.extend("  - " + n for n in notes)
    else:
        lines.append("Предупреждений нет.")

    report_text = "\n".join(lines) + "\n"
    if args.no_report:
        print("")
        print(report_text)
    else:
        rep_dir = os.path.dirname(os.path.abspath(args.report))
        if rep_dir and not os.path.isdir(rep_dir):
            os.makedirs(rep_dir, exist_ok=True)
        with open(args.report, "w", encoding="utf-8", newline="\n") as rf:
            rf.write(report_text)
        print("\nОтчёт записан: %s" % args.report)
        print("Итого: таблиц: %d, INSERT: %d, UPDATE: %d, DDL: CREATE TABLE: %d, "
              "ALTER TABLE: %d"
              % (len(results), total_ins, total_upd, ddl_creates, ddl_alters))

    print("Выходной SQL записан: %s" % args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
