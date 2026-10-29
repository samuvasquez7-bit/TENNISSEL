import random
import math
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path


_MYSQL_PLACEHOLDER = re.compile(r"%s")
_INFORMATION_SCHEMA = re.compile(r"\bINFORMATION_SCHEMA\.COLUMNS\b", re.IGNORECASE)
_DATE_INTERVAL = re.compile(
    r"\bDATE_(ADD|SUB)\(\s*([A-Z_][A-Z_0-9]*|[A-Z_][A-Z_0-9]*\([^()]*\))"
    r"\s*,\s*INTERVAL\s+(\?|\d+)\s+(DAY|HOUR|MINUTE|MONTH|YEAR)\s*\)",
    re.IGNORECASE,
)


def _decode_sqlite_value(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


sqlite3.register_converter("DATE", lambda value: date.fromisoformat(_decode_sqlite_value(value)))
sqlite3.register_converter("DATETIME", lambda value: datetime.fromisoformat(_decode_sqlite_value(value)))
sqlite3.register_converter("TIMESTAMP", lambda value: datetime.fromisoformat(_decode_sqlite_value(value)))
sqlite3.register_converter("TIME", lambda value: time.fromisoformat(_decode_sqlite_value(value)))
sqlite3.register_converter("DECIMAL", lambda value: Decimal(_decode_sqlite_value(value)))


class Row(tuple):
    def __new__(cls, values, names):
        instance = super().__new__(cls, values)
        instance._names = tuple(names)
        instance._values_by_name = dict(zip(instance._names, instance))
        return instance

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values_by_name[key]
        return super().__getitem__(key)

    def get(self, key, default=None):
        return self._values_by_name.get(key, default)

    def keys(self):
        return self._names


class DictionaryRow(dict):
    def __init__(self, values, names):
        super().__init__(zip(names, values))
        self._names = tuple(names)

    def __getitem__(self, key):
        if isinstance(key, int):
            key = self._names[key]
        return super().__getitem__(key)


class SQLiteCursor:
    def __init__(self, connection, dictionary=False):
        self._connection = connection
        self._cursor = connection._connection.cursor()
        self._dictionary = dictionary
        self._override_rows = None
        self._override_index = 0
        self._override_description = None

    def execute(self, query, parameters=()):
        self._override_rows = None
        self._override_description = None
        if _INFORMATION_SCHEMA.search(query):
            self._execute_column_metadata(query, parameters)
            return self

        modify_match = re.match(
            r"ALTER\s+TABLE\s+`?(\w+)`?\s+MODIFY(?:\s+COLUMN)?\s+`?(\w+)`?"
            r"\s+.+?\s+(NOT\s+NULL|NULL)\s*$",
            query.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if modify_match:
            table_name, column_name, nullability = modify_match.groups()
            safe_table_name = table_name.replace('"', '""')
            columns = self._connection._connection.execute(
                f'PRAGMA table_info("{safe_table_name}")'
            ).fetchall()
            column = next(
                (item for item in columns if item["name"] == column_name),
                None,
            )
            if column is None:
                raise sqlite3.OperationalError(
                    f"Cannot modify missing column {table_name}.{column_name}"
                )
            wants_not_null = nullability.upper() == "NOT NULL"
            if bool(column["notnull"]) != wants_not_null:
                raise sqlite3.OperationalError(
                    f"SQLite cannot change nullability of {table_name}.{column_name} "
                    "without rebuilding the table."
                )
            return self

        add_columns_match = re.match(
            r"ALTER\s+TABLE\s+`?(\w+)`?\s+ADD\s+COLUMN\s+(.*)",
            query.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if add_columns_match:
            table_name, definitions = add_columns_match.groups()
            additions = re.split(
                r",\s*ADD\s+COLUMN\s+",
                definitions,
                flags=re.IGNORECASE,
            )
            if len(additions) > 1:
                for definition in additions:
                    translated = translate_sql(
                        f'ALTER TABLE "{table_name}" ADD COLUMN {definition}'
                    )
                    self._cursor.execute(translated, _adapt_parameters(parameters))
                return self

        if re.search(r"\bFOR\s+UPDATE\b", query, re.IGNORECASE):
            self._connection.begin(write=True)

        translated = translate_sql(query)
        self._cursor.execute(translated, _adapt_parameters(parameters))
        return self

    def executemany(self, query, parameters):
        self._override_rows = None
        self._cursor.executemany(
            translate_sql(query),
            (_adapt_parameters(row) for row in parameters),
        )
        return self

    def fetchone(self):
        if self._override_rows is not None:
            if self._override_index >= len(self._override_rows):
                return None
            row = self._override_rows[self._override_index]
            self._override_index += 1
            return self._convert_row(row, self._override_description)

        row = self._cursor.fetchone()
        if row is None:
            return None
        return self._convert_row(row, self._cursor.description)

    def fetchall(self):
        if self._override_rows is not None:
            rows = self._override_rows[self._override_index:]
            self._override_index = len(self._override_rows)
            return [
                self._convert_row(row, self._override_description)
                for row in rows
            ]
        return [
            self._convert_row(row, self._cursor.description)
            for row in self._cursor.fetchall()
        ]

    def fetchmany(self, size=None):
        if size is None:
            size = self._cursor.arraysize
        if self._override_rows is not None:
            end = min(self._override_index + size, len(self._override_rows))
            rows = self._override_rows[self._override_index:end]
            self._override_index = end
            return [
                self._convert_row(row, self._override_description)
                for row in rows
            ]
        return [
            self._convert_row(row, self._cursor.description)
            for row in self._cursor.fetchmany(size)
        ]

    def _execute_column_metadata(self, query, parameters):
        values = iter(parameters or ())

        def metadata_value(field):
            match = re.search(
                rf"\b{field}\s*=\s*(?:'([^']*)'|%s|\?)",
                query,
                re.IGNORECASE,
            )
            if not match:
                return None
            if match.group(1) is not None:
                return match.group(1)
            return next(values, None)

        table_name = metadata_value("TABLE_NAME")
        column_name = metadata_value("COLUMN_NAME")
        if not table_name:
            raise sqlite3.OperationalError(
                "INFORMATION_SCHEMA query is missing TABLE_NAME"
            )

        safe_table_name = str(table_name).replace('"', '""')
        columns = self._connection._connection.execute(
            f'PRAGMA table_info("{safe_table_name}")'
        ).fetchall()
        selected = next(
            (column for column in columns if column["name"] == column_name),
            None,
        )

        if re.search(r"\bCOUNT\s*\(\s*\*\s*\)", query, re.IGNORECASE):
            count = int(selected is not None) if column_name else len(columns)
            self._override_rows = [(count,)]
            self._override_description = (("COUNT(*)",),)
        else:
            select_match = re.search(
                r"\bSELECT\s+(.*?)\s+FROM\s+INFORMATION_SCHEMA\.COLUMNS\b",
                query,
                re.IGNORECASE | re.DOTALL,
            )
            if not select_match:
                raise sqlite3.OperationalError("Unsupported INFORMATION_SCHEMA query")

            selected_columns = []
            for selection in select_match.group(1).split(","):
                field_match = re.match(
                    r"\s*([A-Za-z_][A-Za-z_0-9]*)"
                    r"(?:\s+AS\s+([A-Za-z_][A-Za-z_0-9]*))?\s*$",
                    selection,
                    re.IGNORECASE,
                )
                if not field_match:
                    raise sqlite3.OperationalError(
                        f"Unsupported INFORMATION_SCHEMA field: {selection.strip()}"
                    )
                field, alias = field_match.groups()
                selected_columns.append((field.upper(), alias or field))

            metadata = []
            if selected is not None:
                metadata = [selected]
            elif column_name is None:
                metadata = columns

            field_values = []
            field_names = []
            for field, alias in selected_columns:
                field_names.append(alias)
            for item in metadata:
                declared_type = str(item["type"] or "").split("(", 1)[0].upper()
                values_by_field = {
                    "COLUMN_NAME": item["name"],
                    "IS_NULLABLE": "NO" if item["notnull"] else "YES",
                    "DATA_TYPE": declared_type,
                    "COLUMN_TYPE": item["type"],
                    "COLUMN_DEFAULT": item["dflt_value"],
                    "ORDINAL_POSITION": item["cid"] + 1,
                    "EXTRA": "",
                }
                unsupported = [
                    field for field, _ in selected_columns
                    if field not in values_by_field
                ]
                if unsupported:
                    raise sqlite3.OperationalError(
                        f"Unsupported INFORMATION_SCHEMA field: {unsupported[0]}"
                    )
                field_values.append(tuple(values_by_field[field] for field, _ in selected_columns))
            self._override_rows = field_values
            self._override_description = tuple((name,) for name in field_names)
        self._override_index = 0

    def _convert_row(self, row, description):
        names = tuple(column[0] for column in description or ())
        values = tuple(row)
        if self._dictionary:
            return DictionaryRow(values, names)
        return Row(values, names)

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._override_description or self._cursor.description

    def close(self):
        self._cursor.close()


class SQLiteConnection:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self, dictionary=False, **_options):
        return SQLiteCursor(self, dictionary=dictionary)

    def execute(self, query, parameters=()):
        return self.cursor().execute(query, parameters)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def begin(self, write=False):
        if not self._connection.in_transaction:
            mode = "IMMEDIATE" if write else "DEFERRED"
            self._connection.execute(f"BEGIN {mode}")

    def start_transaction(self):
        self.begin(write=True)

    def is_connected(self):
        return True


def _adapt_parameters(parameters):
    if parameters is None:
        return ()
    if isinstance(parameters, dict):
        return {
            key: _adapt_value(value)
            for key, value in parameters.items()
        }
    return tuple(_adapt_value(value) for value in parameters)


def _adapt_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _concat(*values):
    return "".join("" if value is None else str(value) for value in values)


def _lpad(value, width, padding):
    value = str(value)
    padding = str(padding or " ")
    if len(value) >= int(width) or not padding:
        return value
    needed = int(width) - len(value)
    return (padding * ((needed + len(padding) - 1) // len(padding)) + value)[-int(width):]


def _add_time(value, offset):
    if value is None or offset is None:
        return None
    start = datetime.strptime(str(value), "%H:%M:%S" if len(str(value)) > 5 else "%H:%M")
    delta = datetime.strptime(str(offset), "%H:%M:%S" if len(str(offset)) > 5 else "%H:%M")
    total = start + timedelta(
        hours=delta.hour,
        minutes=delta.minute,
        seconds=delta.second,
    )
    return total.strftime("%H:%M:%S")


def _time_format(value, format_string):
    if value is None:
        return None
    parsed = datetime.strptime(str(value), "%H:%M:%S" if len(str(value)) > 5 else "%H:%M")
    return (
        str(format_string)
        .replace("%H", parsed.strftime("%H"))
        .replace("%i", parsed.strftime("%M"))
        .replace("%s", parsed.strftime("%S"))
    )


def _rand():
    return random.random()


def _elt(index, *values):
    try:
        return values[int(index) - 1]
    except (IndexError, TypeError, ValueError):
        return None


def _if(condition, true_value, false_value):
    return true_value if condition else false_value


def _least(*values):
    return None if any(value is None for value in values) else min(values)


def _substring_index(value, delimiter, count):
    if value is None or delimiter is None or not delimiter:
        return None if value is None or delimiter is None else ""
    pieces = str(value).split(str(delimiter))
    count = int(count)
    if count > 0:
        return str(delimiter).join(pieces[:count])
    if count < 0:
        return str(delimiter).join(pieces[count:])
    return ""


def translate_sql(query):
    translated = _MYSQL_PLACEHOLDER.sub("?", query)
    translated = re.sub(
        r"\bINSERT\s+IGNORE\s+INTO\b",
        "INSERT OR IGNORE INTO",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b",
        "ON CONFLICT DO UPDATE SET",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bVALUES\s*\(\s*([A-Za-z_][A-Za-z_0-9]*)\s*\)",
        r"excluded.\1",
        translated,
        flags=re.IGNORECASE,
    )
    conflict_match = re.search(r"\bON\s+CONFLICT\b", translated, re.IGNORECASE)
    if conflict_match:
        insert_select = re.search(r"\bINSERT\b.*?\bSELECT\b", translated[:conflict_match.start()], re.IGNORECASE | re.DOTALL)
        if insert_select:
            select_sql = translated[insert_select.end():conflict_match.start()]
            if not re.search(r"\bWHERE\b", select_sql, re.IGNORECASE):
                translated = (
                    translated[:conflict_match.start()].rstrip()
                    + " WHERE 1 "
                    + translated[conflict_match.start():]
                )
    translated = re.sub(r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bCURDATE\s*\(\s*\)", "date('now')", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bFOR\s+UPDATE\b", "", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"\bCONVERT\(\s*([^()]+?)\s+USING\s+\w+\s*\)",
        r"\1",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bCOLLATE\s+utf8mb4_[A-Za-z0-9_]+",
        "COLLATE NOCASE",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\s+(?:AFTER|FIRST)\s+`?\w+`?\s*(?=,|$)",
        "",
        translated,
        flags=re.IGNORECASE,
    )

    def replace_interval(match):
        direction, expression, amount, unit = match.groups()
        sign = "+" if direction.upper() == "ADD" else "-"
        unit = unit.lower() + "s"
        if amount == "?":
            modifier = f"'{sign}' || ? || ' {unit}'"
        else:
            modifier = f"'{sign}{amount} {unit}'"
        function = "date" if unit == "days" else "datetime"
        return f"{function}({expression}, {modifier})"

    translated = _DATE_INTERVAL.sub(replace_interval, translated)
    translated = re.sub(
        r"\bUPDATE\s+productos\s+p\s+JOIN\s+tmp_producto_ofertas_empresa\s+oe"
        r"\s+ON\s+oe\.producto_id\s*=\s*p\.id\s+SET\s+"
        r"p\.precio_anterior\s*=\s*oe\.precio_regular\s*,\s*"
        r"p\.precio\s*=\s*ROUND\(oe\.precio_regular\s*\*\s*"
        r"\(100\s*-\s*oe\.porcentaje_descuento\)\s*/\s*100\s*,\s*0\)\s*,\s*"
        r"p\.en_oferta\s*=\s*1\s*,\s*p\.fecha_fin_oferta\s*=\s*NULL",
        """UPDATE productos AS p
           SET precio_anterior = (
                   SELECT oe.precio_regular FROM tmp_producto_ofertas_empresa oe
                   WHERE oe.producto_id = p.id
               ),
               precio = (
                   SELECT ROUND(oe.precio_regular * (100 - oe.porcentaje_descuento) / 100, 0)
                   FROM tmp_producto_ofertas_empresa oe
                   WHERE oe.producto_id = p.id
               ),
               en_oferta = 1,
               fecha_fin_oferta = NULL
           WHERE EXISTS (
               SELECT 1 FROM tmp_producto_ofertas_empresa oe
               WHERE oe.producto_id = p.id
           )""",
        translated,
        flags=re.IGNORECASE | re.DOTALL,
    )
    translated = re.sub(
        r"\bUPDATE\s+reservas\s+r\s+JOIN\s+usuarios\s+u\s+ON\b.*?"
        r"\bSET\s+r\.user_id\s*=\s*u\.id\s+WHERE\s+r\.user_id\s+IS\s+NULL",
        """UPDATE reservas AS r
           SET user_id = (
               SELECT u.id FROM usuarios AS u
               WHERE r.nombre_usuario = u.username COLLATE NOCASE
                  OR r.nombre_usuario = u.nombre_completo COLLATE NOCASE
               ORDER BY u.id LIMIT 1
           )
           WHERE r.user_id IS NULL
             AND EXISTS (
               SELECT 1 FROM usuarios AS u
               WHERE r.nombre_usuario = u.username COLLATE NOCASE
                  OR r.nombre_usuario = u.nombre_completo COLLATE NOCASE
             )""",
        translated,
        flags=re.IGNORECASE | re.DOTALL,
    )
    translated = re.sub(
        r"\(\s*SELECT\s+GROUP_CONCAT\(e\.estado\s+ORDER\s+BY\s+e\.created_at\s+"
        r"DESC\s+SEPARATOR\s+('(?:''|[^'])*')\)\s+FROM\s+marketplace_envios\s+e\s+"
        r"WHERE\s+e\.factura_id\s*=\s*f\.id\s*\)",
        lambda match: (
            "(SELECT GROUP_CONCAT(ordered.estado, "
            + match.group(1)
            + ") FROM (SELECT e.estado FROM marketplace_envios e "
            "WHERE e.factura_id = f.id ORDER BY e.created_at DESC) AS ordered)"
        ),
        translated,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if re.match(r"^\s*CREATE\s+(?:TEMPORARY\s+)?TABLE\b", translated, re.IGNORECASE):
        translated = _sqlite_ddl(translated)
    return translated


def _split_sql_statements(script):
    statement = []
    quote = None
    index = 0
    while index < len(script):
        char = script[index]
        next_char = script[index + 1] if index + 1 < len(script) else ""

        if quote:
            statement.append(char)
            if char == "\\" and quote != "`" and next_char:
                statement.append(next_char)
                index += 2
                continue
            if char == quote:
                if next_char == quote:
                    statement.append(next_char)
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char in ("'", '"', "`"):
            quote = char
            statement.append(char)
        elif char == "/" and next_char == "*":
            end = script.find("*/", index + 2)
            if end == -1:
                raise ValueError("Unterminated SQL block comment in schema")
            statement.append(" ")
            index = end + 2
            continue
        elif char == "#" or (
            char == "-"
            and next_char == "-"
            and (index + 2 == len(script) or script[index + 2].isspace())
        ):
            end = script.find("\n", index)
            index = len(script) if end == -1 else end + 1
            statement.append("\n")
            continue
        elif char == ";":
            value = "".join(statement).strip()
            if value:
                yield value
            statement = []
        else:
            statement.append(char)
        index += 1

    value = "".join(statement).strip()
    if value:
        yield value


def _sqlite_ddl(statement):
    statement = re.sub(
        r"`?([A-Za-z_][A-Za-z_0-9]*)`?\s+(?:BIGINT|INT|INTEGER|SMALLINT)"
        r"(?:\s*\(\s*\d+\s*\))?\s+(?:NOT\s+NULL\s+)?AUTO_INCREMENT"
        r"\s+PRIMARY\s+KEY",
        r"\1 INTEGER PRIMARY KEY AUTOINCREMENT",
        statement,
        flags=re.IGNORECASE,
    )
    statement = re.sub(
        r"`?([A-Za-z_][A-Za-z_0-9]*)`?\s+(?:BIGINT|INT|INTEGER|SMALLINT)"
        r"(?:\s*\(\s*\d+\s*\))?\s+(?:NOT\s+NULL\s+)?AUTO_INCREMENT",
        r"\1 INTEGER",
        statement,
        flags=re.IGNORECASE,
    )
    statement = re.sub(
        r"\bENUM\s*\([^)]*\)",
        "TEXT",
        statement,
        flags=re.IGNORECASE,
    )
    statement = re.sub(r"\bTINYINT\s*\(\s*1\s*\)", "INTEGER", statement, flags=re.IGNORECASE)
    statement = re.sub(r"\bUNSIGNED\b", "", statement, flags=re.IGNORECASE)
    statement = re.sub(
        r"\bUNIQUE\s+(?:KEY|INDEX)\s+`?[A-Za-z_][A-Za-z_0-9]*`?\s*(\([^)]*\))",
        r"UNIQUE \1",
        statement,
        flags=re.IGNORECASE,
    )
    statement = re.sub(
        r",\s*(?:KEY|INDEX)\s+`?[A-Za-z_][A-Za-z_0-9]*`?\s*\([^)]*\)",
        "",
        statement,
        flags=re.IGNORECASE,
    )
    statement = re.sub(
        r"\s+ON\s+UPDATE\s+CURRENT_TIMESTAMP(?:\(\))?",
        "",
        statement,
        flags=re.IGNORECASE,
    )
    statement = re.sub(
        r"\)\s*ENGINE\s*=\s*[A-Za-z0-9_]+.*$",
        ")",
        statement,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return statement.strip()


def _schema_statements(schema_path, seed_data):
    script = Path(schema_path).read_text(encoding="utf-8")
    known_tables = {}
    for statement in _split_sql_statements(script):
        if re.match(r"^(?:CREATE\s+DATABASE|USE\s+|DROP\s+TABLE)\b", statement, re.IGNORECASE):
            continue

        if re.match(r"^DROP\s+TEMPORARY\s+TABLE\b", statement, re.IGNORECASE):
            if not seed_data:
                continue
            yield re.sub(
                r"^DROP\s+TEMPORARY\s+TABLE",
                "DROP TABLE",
                statement,
                count=1,
                flags=re.IGNORECASE,
            )
            continue

        if re.match(r"^CREATE\s+TEMPORARY\s+TABLE\b", statement, re.IGNORECASE):
            if not seed_data:
                continue
            yield _sqlite_ddl(
                re.sub(
                    r"^CREATE\s+TEMPORARY\s+TABLE",
                    "CREATE TEMPORARY TABLE",
                    statement,
                    count=1,
                    flags=re.IGNORECASE,
                )
            )
            continue

        like_match = re.match(
            r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+`?(\w+)`?\s+LIKE\s+`?(\w+)`?",
            statement,
            re.IGNORECASE,
        )
        if like_match:
            target, source = like_match.groups()
            source_ddl = known_tables.get(source.lower())
            if source_ddl is None:
                raise sqlite3.OperationalError(
                    f"Schema table {source!r} must be declared before it is cloned"
                )
            statement = re.sub(
                r"^(CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+`?\w+`?",
                rf"\1 `{target}`",
                source_ddl,
                count=1,
                flags=re.IGNORECASE,
            )

        if re.match(r"^ALTER\s+TABLE\b", statement, re.IGNORECASE):
            if re.search(r"\bMODIFY\b", statement, re.IGNORECASE):
                continue
            alter_match = re.match(
                r"ALTER\s+TABLE\s+`?(\w+)`?\s+(.*)",
                statement,
                re.IGNORECASE | re.DOTALL,
            )
            if not alter_match or not re.match(r"ADD\b", alter_match.group(2), re.IGNORECASE):
                continue
            table, operations = alter_match.groups()
            if re.match(r"ADD\s+CONSTRAINT\b", operations, re.IGNORECASE):
                continue
            operations = re.sub(r"^ADD\s+COLUMN\s+", "", operations, flags=re.IGNORECASE)
            additions = re.split(r",\s*ADD\s+COLUMN\s+", operations, flags=re.IGNORECASE)
            for addition in additions:
                addition = re.sub(
                    r"\s+(?:AFTER|FIRST)\s+`?\w+`?\s*$",
                    "",
                    addition,
                    flags=re.IGNORECASE,
                )
                yield f'ALTER TABLE "{table}" ADD COLUMN {addition}'
            continue

        if re.match(r"^CREATE\s+TABLE\b", statement, re.IGNORECASE):
            converted = _sqlite_ddl(statement)
            table_match = re.match(
                r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+`?(\w+)`?",
                converted,
                re.IGNORECASE,
            )
            if table_match:
                known_tables[table_match.group(1).lower()] = converted
            yield converted
            continue

        if seed_data and re.match(r"^(?:INSERT|UPDATE)\b", statement, re.IGNORECASE):
            yield translate_sql(statement)


def _register_functions(connection):
    connection.create_function("CONCAT", -1, _concat)
    connection.create_function("LPAD", 3, _lpad)
    connection.create_function("ADDTIME", 2, _add_time)
    connection.create_function("TIME_FORMAT", 2, _time_format)
    connection.create_function("RAND", 0, _rand)
    connection.create_function("ELT", -1, _elt)
    connection.create_function("FLOOR", 1, math.floor)
    connection.create_function("IF", 3, _if)
    connection.create_function("LEAST", -1, _least)
    connection.create_function("SUBSTRING_INDEX", 3, _substring_index)


def initialize_database(connection, schema_path):
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if current_version >= 1:
        return

    connection.begin(write=True)
    try:
        existing_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        seed_data = not existing_tables
        existing_table_names = {table["name"].lower() for table in existing_tables}
        for statement in _schema_statements(schema_path, seed_data):
            if not seed_data and re.match(
                r"^(?:CREATE|DROP)\s+TEMPORARY\s+TABLE\b",
                statement,
                re.IGNORECASE,
            ):
                continue

            create_match = re.match(
                r"CREATE\s+(?:TEMPORARY\s+)?TABLE"
                r"(?:\s+IF\s+NOT\s+EXISTS)?\s+`?(\w+)`?",
                statement,
                re.IGNORECASE,
            )
            if create_match and not re.match(
                r"^CREATE\s+TEMPORARY\s+TABLE\b",
                statement,
                re.IGNORECASE,
            ):
                if create_match.group(1).lower() in existing_table_names:
                    continue

            add_column_match = re.match(
                r'ALTER\s+TABLE\s+"?(\w+)"?\s+ADD\s+COLUMN\s+`?(\w+)`?',
                statement,
                re.IGNORECASE,
            )
            if add_column_match:
                table_name, column_name = add_column_match.groups()
                safe_table_name = table_name.replace('"', '""')
                columns = connection._connection.execute(
                    f'PRAGMA table_info("{safe_table_name}")'
                ).fetchall()
                if any(column["name"].lower() == column_name.lower() for column in columns):
                    continue

            connection.execute(statement)
            if create_match and not re.match(
                r"^CREATE\s+TEMPORARY\s+TABLE\b",
                statement,
                re.IGNORECASE,
            ):
                existing_table_names.add(create_match.group(1).lower())
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def connect(database_path, schema_path):
    database_path = str(database_path)
    if database_path != ":memory:" and not database_path.startswith("file:"):
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)

    raw_connection = sqlite3.connect(
        database_path,
        timeout=30,
        isolation_level="DEFERRED",
        check_same_thread=False,
        uri=database_path.startswith("file:"),
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    raw_connection.row_factory = sqlite3.Row
    raw_connection.execute("PRAGMA foreign_keys = ON")
    raw_connection.execute("PRAGMA busy_timeout = 30000")
    raw_connection.execute("PRAGMA journal_mode = WAL")
    _register_functions(raw_connection)

    connection = SQLiteConnection(raw_connection)
    try:
        initialize_database(connection, schema_path)
    except Exception:
        connection.close()
        raise
    return connection
