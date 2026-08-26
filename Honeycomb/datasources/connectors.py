"""
Opening a connection to a customer's database and running one statement on it.

This is the layer the SQL console sits on. Everything dangerous about a query
IDE lives here, so the reasoning is written down rather than implied.

WHAT BOUNDS A QUERY
-------------------
Not a parser. Blocking statements by looking for the word DELETE is the
approach that feels safest and is worth the least: ``WITH x AS (DELETE FROM t
RETURNING *) SELECT * FROM x`` starts with SELECT, ``/*hi*/drop`` defeats a
prefix check, and every rule added is one more thing between a user and a
legitimate query. What actually bounds a query is the *privilege of the role
the connection authenticates as*. Give each source a role that owns nothing
and has SELECT on what it should read, and the destructive query fails at the
server whatever it looks like.

Four things are enforced here on top of that, in the order they take effect:

1. `guard_target` refuses to dial Honeycomb's own database. A DataSource is
   rows a user types; without this, someone points one at localhost:5432 and
   the "customer warehouse" they query is the table holding every tenant.
2. The transaction is opened read-only and then *sealed* -- see each dialect's
   `seal`, and read the MySQL one before assuming the two engines are
   equivalent, because they are not.
3. A statement timeout is set on the connection, so a runaway query is killed
   by the server. A timeout enforced in Python only frees Python; the
   warehouse keeps burning.
4. Rows are streamed, not fetched. Both drivers buffer an entire result set
   into memory by default, which would make the row cap a display limit rather
   than a memory bound -- ``select * from events`` on a billion-row table would
   be in this process before the cap ever ran. Postgres uses libpq's single-row
   mode; MySQL uses an unbuffered cursor. The cap then stops the read.

WHAT IS STILL NOT COVERED
-------------------------
A read-only transaction does not stop a *superuser* role reading files through
``pg_read_file()``, reaching another host through ``dblink``, or leaking data
through a timing side channel. That is what point (1) above means about
privilege: this module can bound a well-privileged role and can do nothing at
all about a badly-privileged one. There is also no egress control -- a source
can name any host, so on a public deployment this reaches the internal
network. Put it behind an allow-list before that day.
"""

import datetime
import decimal
import ipaddress
import re
import time
import uuid
from contextlib import contextmanager

from django.conf import settings
from django.db import connections

from .models import DataSource
from .secrets import SecretError, resolve

try:  # psycopg 3 is the project's own driver; the import is guarded so a
    # machine without it still runs the rest of the app.
    import psycopg
except ImportError:  # pragma: no cover - exercised only on a broken install
    psycopg = None

try:
    import pymysql
    from pymysql.constants import FIELD_TYPE
    from pymysql.cursors import SSCursor
except ImportError:  # pragma: no cover
    pymysql = None
    FIELD_TYPE = None
    SSCursor = None


class ConnectorError(Exception):
    """Anything that stopped a query from producing rows.

    The message is shown to the person who pressed Run, so it says what to do
    next rather than quoting a traceback.
    """


class UnsupportedSource(ConnectorError):
    """No driver is wired up for this source kind."""


class ForbiddenTarget(ConnectorError):
    """The source points somewhere this app refuses to connect."""


class QueryFailed(ConnectorError):
    """The database rejected the statement, or the connection dropped."""


# Defaults. Overridable in settings so a deployment can tighten them without
# touching this file; every one of them is a ceiling, never a target.
DEFAULT_MAX_ROWS = 1000
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 8

# Hosts that mean "this machine". Compared after lowercasing; an empty host in
# a libpq connection string also means local, hence the empty string.
LOCAL_HOSTS = frozenset(['', 'localhost', '127.0.0.1', '::1', '0.0.0.0'])


def _setting(name, fallback):
    return getattr(settings, name, fallback)


def max_rows():
    return int(_setting('HONEYCOMB_SQL_MAX_ROWS', DEFAULT_MAX_ROWS))


def timeout_ms():
    return int(_setting('HONEYCOMB_SQL_TIMEOUT_MS', DEFAULT_TIMEOUT_MS))


def max_bytes():
    return int(_setting('HONEYCOMB_SQL_MAX_BYTES', DEFAULT_MAX_BYTES))


def connect_timeout():
    return int(_setting('HONEYCOMB_SQL_CONNECT_TIMEOUT', DEFAULT_CONNECT_TIMEOUT))


def _normalize_host(host):
    host = (host or '').strip().lower()
    # A trailing dot is a fully-qualified name and resolves identically.
    return host.rstrip('.')


def _is_local(host):
    host = _normalize_host(host)
    if host in LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _coerce(value):
    """Make one database value JSON-safe without lying about it.

    Decimal becomes a string rather than a float on purpose: a numeric column
    is usually money or a measurement, and float() silently rounds it. The
    frontend right-aligns it either way; a wrong number that looks right is the
    worst thing a query tool can produce.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN and Infinity are valid float values and invalid JSON.
        if value != value or value in (float('inf'), float('-inf')):
            return str(value)
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        head = raw[:32].hex()
        return '\\x' + head + ('...' if len(raw) > 32 else '')
    if isinstance(value, (list, tuple)):
        return [_coerce(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _coerce(item) for key, item in value.items()}
    return str(value)


def _noop():
    """A release for a stream that has nothing to release."""


class QueryResult(object):
    """What one execution produced. Plain attributes; the serializer shapes it."""

    def __init__(self, columns, rows, row_count, truncated, duration_ms, notice):
        self.columns = columns
        self.rows = rows
        self.row_count = row_count
        self.truncated = truncated
        self.duration_ms = duration_ms
        # A one-line explanation of anything the user should know about the
        # result that the rows themselves do not say -- why it stopped early,
        # or that the statement returned nothing at all.
        self.notice = notice


# ---------------------------------------------------------------------------
# Dialects
# ---------------------------------------------------------------------------
#
# One class per engine. The differences between PostgreSQL and MySQL here are
# not cosmetic -- they change what is actually enforced -- so they live in
# named methods with their own reasoning rather than in `if kind ==` branches
# scattered through the module.


class Dialect(object):
    """What one engine has to answer for the console to work on it."""

    kind = None
    default_port = None

    # -- lifecycle ------------------------------------------------------

    def ensure_driver(self):
        """Raise UnsupportedSource with an installable name if the driver is missing."""
        raise NotImplementedError

    def connect(self, source, password):
        """Open a connection with the timeouts already applied."""
        raise NotImplementedError

    def seal(self, connection):
        """Begin a read-only transaction that the submitted SQL cannot undo.

        Returns the cursor to run on.
        """
        raise NotImplementedError

    def stream(self, cursor, statement):
        """Run `statement` and return (columns, row iterator, release).

        The iterator yields raw driver rows one at a time. `columns` is empty
        for a statement that returns no result set at all.

        `release` must be called once the caller has stopped reading, whether
        it read everything or gave up at the row cap. It is not optional and
        it is not the same as closing the connection: a streaming read leaves
        the connection mid-result, and on PostgreSQL a ROLLBACK issued in that
        state blocks forever waiting for rows nobody is going to ask for.
        """
        raise NotImplementedError

    def abandon(self, connection, cursor):
        """Hang up on a result the caller stopped reading, without draining it."""
        raise NotImplementedError

    def finish(self, connection, cursor):
        """Roll back and close. Called on every path, success included."""
        raise NotImplementedError

    # -- introspection --------------------------------------------------

    def schema_query(self, source):
        """(sql, params) listing the tables and columns this role can see."""
        raise NotImplementedError

    def default_schema(self, source):
        """The schema whose tables need no qualifying in a query."""
        raise NotImplementedError

    def describe(self, cursor):
        """One line naming what answered, for the connection test."""
        raise NotImplementedError

    # -- errors ---------------------------------------------------------

    def clean_error(self, error):
        """A database error worth showing someone."""
        text = str(error).strip() or error.__class__.__name__
        lines = [line for line in text.splitlines() if line.strip()]
        return '\n'.join(lines[:4])[:2000]


class PostgresDialect(Dialect):
    kind = DataSource.Kind.POSTGRES
    default_port = 5432

    def ensure_driver(self):
        if psycopg is None:
            raise UnsupportedSource(
                'The PostgreSQL driver is not installed. '
                'Run: pip install "psycopg[binary]"'
            )

    def connect(self, source, password):
        self.ensure_driver()
        idle_ms = max(timeout_ms(), 1000)
        # The knobs travel in the connection `options` rather than as
        # statements afterwards, so they are in force from the first byte the
        # server executes -- there is no window in which a statement could run
        # untimed.
        options = (
            '-c statement_timeout={0} '
            '-c idle_in_transaction_session_timeout={1} '
            '-c default_transaction_read_only=on'
        ).format(timeout_ms(), idle_ms)

        parameters = {
            'host': source.host or 'localhost',
            'port': source.port or self.default_port,
            'dbname': source.database or '',
            'user': source.username or '',
            'password': password,
            'connect_timeout': connect_timeout(),
            'options': options,
            # The app should not be inventing a hostname for someone else's
            # server, and libpq's default application_name is unhelpfully
            # "psycopg" in the customer's own logs.
            'application_name': 'honeycomb-sql-console',
        }
        if not settings.DEBUG:
            # In production a warehouse connection crossing a network
            # unencrypted is not acceptable. Left off in DEBUG so a local
            # docker Postgres, which usually has no certificate, still works.
            parameters['sslmode'] = 'require'

        try:
            return psycopg.connect(autocommit=False, **parameters)
        except Exception as error:
            raise QueryFailed(self.clean_error(error))

    def seal(self, connection):
        """Read-only, then sealed by running a query.

        `SET TRANSACTION` is only legal before the first query of a
        transaction, so running a harmless one immediately afterwards means the
        submitted SQL can no longer flip the mode back. That matters because
        PostgreSQL's simple query protocol -- which psycopg uses for a
        statement with no parameters -- accepts several statements in one
        round trip, so ``SET TRANSACTION READ WRITE; DELETE ...`` is a thing
        someone can send.
        """
        cursor = connection.cursor()
        cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute('SELECT 1')
        cursor.fetchall()
        return cursor

    def stream(self, cursor, statement):
        try:
            generator = cursor.stream(statement)
        except Exception as error:
            raise QueryFailed(self.clean_error(error))

        # Peek one row before reading `description`. In single-row mode the
        # column metadata is not attached to the cursor until the first row
        # arrives, and until then `description` still holds the *previous*
        # statement's columns -- the SELECT 1 from seal(). Reading it early
        # labels every result "?column? / int4".
        try:
            first = next(generator)
        except StopIteration:
            first = None
        except Exception as error:
            generator.close()
            message = str(error)
            if "didn't produce a result" in message:
                # A statement with no result set at all. Under a read-only
                # transaction that is something like SET, not a write -- the
                # write would have been refused.
                return [], iter(()), _noop
            raise QueryFailed(self.clean_error(error))

        if cursor.description is None:
            # Zero rows. PostgreSQL only reveals the column list alongside a
            # row in this mode, so an empty result has no headers to show. The
            # trade is deliberate: streaming is what makes the row cap a real
            # memory bound, and "no rows" is a complete answer without them.
            generator.close()
            return [], iter(()), _noop

        columns = [
            {'name': column.name, 'type': self._type_name(column)}
            for column in cursor.description
        ]

        def rows():
            if first is not None:
                yield first
            for record in generator:
                yield record

        # `generator.close` is the release: it tells psycopg to stop expecting
        # rows, which is what lets the ROLLBACK afterwards return.
        return columns, rows(), generator.close

    def _type_name(self, column):
        """'text' rather than oid 25. The oid beats nothing when unknown."""
        try:
            info = psycopg.postgres.types.get(column.type_code)
            if info is not None:
                return info.name
        except Exception:
            pass
        return str(column.type_code)

    def abandon(self, connection, cursor):
        # The caller has already run the release from `stream`, so the
        # connection is out of single-row mode and an ordinary rollback works.
        self.finish(connection, cursor)

    def finish(self, connection, cursor):
        try:
            connection.rollback()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass

    def schema_query(self, source):
        return (
            """
            SELECT c.table_schema, c.table_name, c.column_name, c.data_type,
                   t.table_type
            FROM information_schema.columns AS c
            JOIN information_schema.tables AS t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
              AND t.table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            LIMIT %s
            """,
            (SCHEMA_COLUMN_LIMIT,),
        )

    def default_schema(self, source):
        # 'public' is on the default search_path, so qualifying it adds noise
        # to every completion for no gain.
        return 'public'

    def describe(self, cursor):
        cursor.execute('SELECT current_database(), current_user, version()')
        database, user, version = cursor.fetchone()
        match = re.search(r'PostgreSQL\s+([0-9]+(?:\.[0-9]+)?)', version or '')
        release = 'PostgreSQL ' + match.group(1) if match else 'PostgreSQL'
        return 'Connected to {0} as {1} on {2}.'.format(database, user, release)

    def clean_error(self, error):
        """psycopg repeats "connection to server at ..." per address family.

        The password is never in there -- libpq redacts it -- but the user and
        host are, and four lines of the same message is noise either way.
        """
        return super(PostgresDialect, self).clean_error(error)


class MySQLDialect(Dialect):
    """
    MySQL and MariaDB.

    **Read this before assuming it behaves like the PostgreSQL dialect.**
    ``START TRANSACTION READ ONLY`` in MySQL blocks DML and *not DDL*. Verified
    against MySQL 8.4: inside a sealed read-only transaction, a user with the
    privilege can still run CREATE TABLE, ALTER TABLE, TRUNCATE and DROP TABLE.
    DDL implicitly commits and is simply not covered by the transaction's
    read-only mode.

    So on MySQL the read-only transaction is a guard against accidents, not a
    boundary. The GRANT is the boundary, and it is not optional: give the
    source a user with SELECT and nothing else, or a query typed into the
    console can drop a table. The same is true on PostgreSQL in principle and
    false in practice -- there the transaction does refuse DDL as well.

    Three things do hold here, and two of them hold better than on PostgreSQL:

    * ``SET TRANSACTION READ WRITE`` inside a transaction is an error (1568),
      so the seal cannot be lifted at all rather than merely being awkward to
      lift. ``SET SESSION transaction_read_only = 0`` is accepted but applies
      to the *next* transaction, so it does not free this one either.
    * Multi-statement submissions are rejected by the protocol: PyMySQL does
      not negotiate CLIENT_MULTI_STATEMENTS, so ``SELECT 1; DROP TABLE t`` is
      a syntax error rather than two statements.
    * Unlike PostgreSQL's single-row mode, MySQL sends the column metadata
      before the rows, so an empty result still has headers.
    """

    kind = DataSource.Kind.MYSQL
    default_port = 3306

    def ensure_driver(self):
        if pymysql is None:
            raise UnsupportedSource(
                'The MySQL driver is not installed. '
                'Run: pip install PyMySQL cryptography'
            )

    def connect(self, source, password):
        self.ensure_driver()
        parameters = {
            'host': source.host or 'localhost',
            'port': int(source.port or self.default_port),
            'user': source.username or '',
            'password': password,
            'database': source.database or '',
            'connect_timeout': connect_timeout(),
            # A hard client-side stop, because MySQL's own statement timer does
            # not cover everything (see _apply_timeout). Padded past the server
            # timeout so the server gets first refusal and the nicer error.
            'read_timeout': max(1, timeout_ms() // 1000) + 5,
            'write_timeout': connect_timeout(),
            'autocommit': False,
            'charset': 'utf8mb4',
            # Left at PyMySQL's default client flags on purpose: they do not
            # include CLIENT_MULTI_STATEMENTS, and that absence is one of the
            # guarantees this dialect relies on.
        }
        if not settings.DEBUG:
            # Encrypt without verifying, which is what sslmode=require does on
            # the PostgreSQL side. Real verification needs a CA path, and this
            # model has no field to put one in -- add ssl_ca here alongside a
            # column for it when a deployment needs it.
            parameters['ssl'] = {'ca': None}

        try:
            connection = pymysql.connect(**parameters)
        except Exception as error:
            raise QueryFailed(self.clean_error(error))

        self._apply_timeout(connection)
        return connection

    def _apply_timeout(self, connection):
        """Ask the server to kill a long statement.

        `max_execution_time` (MySQL 5.7.8+) is milliseconds and applies to
        read-only SELECTs, which is what this console runs. MariaDB spells it
        `max_statement_time` and takes seconds. Neither exists on the other, so
        both are attempted and a failure is not fatal -- the client-side
        read_timeout and the KILL QUERY in `_stop` are the backstop.

        Verified: `max_execution_time` does interrupt a real scan (error 3024).
        It does *not* interrupt `SELECT SLEEP(n)`, which is why the backstop
        exists rather than being belt and braces.
        """
        seconds = timeout_ms() / 1000.0
        for statement in (
            'SET SESSION max_execution_time = {0}'.format(timeout_ms()),
            'SET SESSION max_statement_time = {0}'.format(seconds),
        ):
            try:
                with connection.cursor() as cursor:
                    cursor.execute(statement)
            except Exception:
                # The other engine's spelling. Expected, not a problem.
                pass

    def seal(self, connection):
        cursor = connection.cursor(SSCursor)
        cursor.execute('START TRANSACTION READ ONLY')
        # Not strictly needed -- MySQL refuses SET TRANSACTION inside a
        # transaction outright -- but it keeps the two dialects the same shape
        # and proves the connection round-trips before the user's statement.
        cursor.execute('SELECT 1')
        cursor.fetchall()
        return cursor

    def stream(self, cursor, statement):
        try:
            cursor.execute(statement)
        except Exception as error:
            raise QueryFailed(self.clean_error(error))

        if cursor.description is None:
            return [], iter(()), _noop

        columns = [
            {'name': column[0], 'type': MYSQL_TYPES.get(column[1], str(column[1]))}
            for column in cursor.description
        ]
        # SSCursor is unbuffered, so iterating it reads from the socket a row
        # at a time and stopping early stops the read. Nothing to release
        # here: `abandon` clears the unbuffered flag and closes the socket,
        # which is what stopping early costs on this driver.
        return columns, iter(cursor), _noop

    def abandon(self, connection, cursor):
        """Hang up mid-result without reading the rest of it.

        PyMySQL's unbuffered cursor insists on draining the remaining packets
        when it is closed, and on `__del__` if it was not -- which for a large
        abandoned result means reading every row off the wire just to discard
        it, and which raises inside `__del__` once the socket is gone,
        producing "Exception ignored in ..." noise in the server log.

        Clearing the flag those two paths loop on lets the connection close
        immediately. It is a private attribute, so it is set defensively: if a
        future PyMySQL renames it, this degrades to the old draining behaviour
        rather than breaking.
        """
        try:
            result = getattr(cursor, '_result', None)
            if result is not None:
                result.unbuffered_active = False
            if getattr(connection, '_result', None) is not None:
                connection._result.unbuffered_active = False
        except Exception:
            pass
        self.finish(connection, cursor)

    def finish(self, connection, cursor):
        try:
            connection.rollback()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass

    def stop(self, source, password, thread_id):
        """Kill a statement the client gave up waiting for.

        A client-side timeout frees this process and leaves the query running
        on the customer's server, still costing them. MySQL lets a user kill
        their own query without any extra privilege, so the honest thing is to
        open one more connection and do it.
        """
        if thread_id is None:
            return
        try:
            killer = pymysql.connect(
                host=source.host or 'localhost',
                port=int(source.port or self.default_port),
                user=source.username or '',
                password=password,
                connect_timeout=connect_timeout(),
                read_timeout=connect_timeout(),
            )
        except Exception:
            return
        try:
            with killer.cursor() as cursor:
                cursor.execute('KILL QUERY {0}'.format(int(thread_id)))
        except Exception:
            pass
        finally:
            try:
                killer.close()
            except Exception:
                pass

    def schema_query(self, source):
        # MySQL has no schemas separate from databases, so `table_schema` is a
        # database name here. The system ones are excluded by name the way
        # pg_catalog is on the other side.
        return (
            """
            SELECT c.table_schema, c.table_name, c.column_name, c.data_type,
                   t.table_type
            FROM information_schema.columns AS c
            JOIN information_schema.tables AS t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema NOT IN
                  ('mysql', 'information_schema', 'performance_schema', 'sys')
              AND t.table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            LIMIT %s
            """,
            (SCHEMA_COLUMN_LIMIT,),
        )

    def default_schema(self, source):
        # The connected database needs no qualifying, the same way `public`
        # does not on PostgreSQL.
        return (source.database or '').strip()

    def describe(self, cursor):
        cursor.execute('SELECT DATABASE(), CURRENT_USER(), VERSION()')
        # fetchall, not fetchone: this cursor is unbuffered, and leaving even a
        # one-row result partly read makes the next statement on the connection
        # warn "Previous unbuffered result was left incomplete".
        database, user, version = cursor.fetchall()[0]
        version = version or ''
        engine = 'MariaDB' if 'mariadb' in version.lower() else 'MySQL'
        match = re.search(r'([0-9]+(?:\.[0-9]+)*)', version)
        release = engine + (' ' + match.group(1) if match else '')
        return 'Connected to {0} as {1} on {2}.'.format(
            database or '(no database)', user, release
        )

    def clean_error(self, error):
        """PyMySQL raises with args of (code, message).

        ``str(error)`` on that is ``(1142, "SELECT command denied ...")`` --
        a Python tuple repr in front of the only useful part. Pulling the
        message out and putting the code after it reads like a database error
        instead of like a bug in this app.
        """
        args = getattr(error, 'args', None)
        if isinstance(args, tuple) and len(args) == 2 and isinstance(args[1], str):
            return '{0} (MySQL error {1})'.format(args[1].strip(), args[0])[:2000]
        return super(MySQLDialect, self).clean_error(error)


# The protocol sends a numeric field type; these are the SQL names people
# recognise. Anything unlisted falls back to the number, which at least does
# not claim to be something it is not.
MYSQL_TYPES = {}
if FIELD_TYPE is not None:
    MYSQL_TYPES = {
        FIELD_TYPE.DECIMAL: 'decimal',
        FIELD_TYPE.NEWDECIMAL: 'decimal',
        FIELD_TYPE.TINY: 'tinyint',
        FIELD_TYPE.SHORT: 'smallint',
        FIELD_TYPE.LONG: 'int',
        FIELD_TYPE.FLOAT: 'float',
        FIELD_TYPE.DOUBLE: 'double',
        FIELD_TYPE.NULL: 'null',
        FIELD_TYPE.TIMESTAMP: 'timestamp',
        FIELD_TYPE.LONGLONG: 'bigint',
        FIELD_TYPE.INT24: 'mediumint',
        FIELD_TYPE.DATE: 'date',
        FIELD_TYPE.TIME: 'time',
        FIELD_TYPE.DATETIME: 'datetime',
        FIELD_TYPE.YEAR: 'year',
        FIELD_TYPE.VARCHAR: 'varchar',
        FIELD_TYPE.VAR_STRING: 'varchar',
        FIELD_TYPE.STRING: 'char',
        FIELD_TYPE.BIT: 'bit',
        FIELD_TYPE.JSON: 'json',
        FIELD_TYPE.ENUM: 'enum',
        FIELD_TYPE.SET: 'set',
        FIELD_TYPE.GEOMETRY: 'geometry',
        # The protocol uses one code for TEXT and BLOB alike and does not say
        # which. Naming both is more honest than picking the likelier one.
        FIELD_TYPE.TINY_BLOB: 'tinytext/blob',
        FIELD_TYPE.MEDIUM_BLOB: 'mediumtext/blob',
        FIELD_TYPE.LONG_BLOB: 'longtext/blob',
        FIELD_TYPE.BLOB: 'text/blob',
    }


DIALECTS = {
    DataSource.Kind.POSTGRES: PostgresDialect(),
    DataSource.Kind.MYSQL: MySQLDialect(),
}


def dialect_for(source):
    dialect = DIALECTS.get(source.kind)
    if dialect is None:
        raise UnsupportedSource(
            '{0} sources have no connector yet. PostgreSQL and MySQL do.'.format(
                source.get_kind_display()
            )
        )
    return dialect


# ---------------------------------------------------------------------------
# Guards and connection
# ---------------------------------------------------------------------------


def guard_target(source):
    """Refuse a source that points at Honeycomb's own database.

    Compared on (host, port, database) against Django's default connection,
    with loopback spellings folded together so 127.0.0.1 does not slip past a
    check written for "localhost".

    This is not paranoia about a hostile user; it is mostly about an honest
    one. Setting up the first source on a laptop, the database whose
    credentials are closest to hand is the app's own, and one careless ``drop
    table accounts_user`` against a role that happens to own it ends the
    tenancy model.
    """
    own = connections['default'].settings_dict
    own_host = _normalize_host(own.get('HOST'))
    own_name = (own.get('NAME') or '').strip()
    own_port = str(own.get('PORT') or '') or '5432'

    dialect = DIALECTS.get(source.kind)
    fallback_port = dialect.default_port if dialect is not None else ''

    host = _normalize_host(source.host)
    port = str(source.port or fallback_port or '')
    database = (source.database or '').strip()

    same_host = host == own_host or (_is_local(host) and _is_local(own_host))
    if same_host and port == own_port and database == own_name:
        raise ForbiddenTarget(
            'This source points at Honeycomb\'s own database. Queries are '
            'never run against it -- point the source at the database you '
            'want to read instead.'
        )


def _password(source):
    try:
        return resolve(source.secret_name)
    except SecretError as error:
        # SecretNotFound already explains what to configure, and never
        # contains the secret itself.
        raise ConnectorError(str(error))


@contextmanager
def _session(source):
    """A connected, sealed, read-only cursor. Always rolled back and closed."""
    dialect = dialect_for(source)
    guard_target(source)
    password = _password(source)
    connection = dialect.connect(source, password)
    cursor = None
    try:
        cursor = dialect.seal(connection)
        yield dialect, cursor
    except ConnectorError:
        raise
    except Exception as error:
        raise QueryFailed(dialect.clean_error(error))
    finally:
        dialect.finish(connection, cursor)


# ---------------------------------------------------------------------------
# Running a statement
# ---------------------------------------------------------------------------


def run_query(source, sql, limit=None):
    """Execute `sql` against `source` and return a QueryResult.

    Blocking: this holds the calling thread for as long as the query runs, up
    to the statement timeout. That is acceptable for a console where a person
    is watching the spinner and unacceptable for anything scheduled -- when
    pipelines start executing, this call belongs on a worker, not on the
    request thread.
    """
    dialect = dialect_for(source)
    guard_target(source)

    statement = (sql or '').strip().rstrip(';').strip()
    if not statement:
        raise QueryFailed('There is no statement to run.')

    cap = max_rows() if limit is None else max(1, min(int(limit), max_rows()))
    budget = max_bytes()
    started = time.monotonic()

    password = _password(source)
    connection = dialect.connect(source, password)
    thread_id = getattr(connection, 'thread_id', lambda: None)()
    cursor = None
    release = _noop
    abandoned = False

    try:
        cursor = dialect.seal(connection)
        try:
            columns, records, release = dialect.stream(cursor, statement)
        except QueryFailed:
            # A client-side timeout leaves the statement running on the
            # customer's server. MySQL lets a user kill their own query, so
            # this stops it rather than leaving it to burn.
            stop = getattr(dialect, 'stop', None)
            if stop is not None and _looks_like_client_timeout(source, started):
                stop(source, password, thread_id)
            raise

        if not columns:
            duration = int((time.monotonic() - started) * 1000)
            return QueryResult([], [], 0, False, duration, 'No rows returned.')

        rows = []
        truncated = False
        notice = ''
        size = 0
        # The loop is guarded, not just the execute above it. Streaming means
        # the server can still refuse the statement *after* it has handed over
        # the column metadata -- a statement timeout on a scan that produces
        # its row at the very end arrives here, and an unwrapped driver
        # exception at this point becomes a 500 instead of the message the
        # person needs to read.
        try:
            for record in records:
                if len(rows) >= cap:
                    truncated = True
                    abandoned = True
                    notice = ('Showing the first {0} rows. Add a LIMIT to see a '
                              'different slice.').format(cap)
                    break
                row = [_coerce(value) for value in record]
                # Rough, and deliberately so: an exact serialized size would
                # mean building the JSON twice. This only has to stop a wide
                # text column from doing what the row cap prevents.
                size += sum(len(str(value)) for value in row) + len(row)
                rows.append(row)
                if size >= budget:
                    truncated = True
                    abandoned = True
                    notice = ('Stopped at {0} rows -- the result passed the {1} MB '
                              'ceiling.').format(len(rows), round(budget / 1048576, 1))
                    break
        except Exception as error:
            # Whatever state the result is in, it is not one to keep reading.
            abandoned = True
            stop = getattr(dialect, 'stop', None)
            if stop is not None and _looks_like_client_timeout(source, started):
                stop(source, password, thread_id)
            raise QueryFailed(dialect.clean_error(error))

        duration = int((time.monotonic() - started) * 1000)
        if not rows and not notice:
            notice = 'The query ran and matched no rows.'
        return QueryResult(columns, rows, len(rows), truncated, duration, notice)
    finally:
        # Release before closing, always. Skipping it on the path where the row
        # cap stopped the read is the bug this ordering exists to prevent: the
        # connection is still mid-result, and the ROLLBACK below then waits for
        # rows nobody will ask for -- forever, on PostgreSQL.
        try:
            release()
        except Exception:
            pass
        # Rollback, not commit, and on the success path too. Nothing a console
        # does should outlive the request.
        if abandoned:
            dialect.abandon(connection, cursor)
        else:
            dialect.finish(connection, cursor)


def _looks_like_client_timeout(source, started):
    """Did we give up waiting, rather than the server refusing?

    Compared against the configured timeout rather than inspecting the driver's
    exception type, because "the socket went quiet after roughly the timeout"
    is the same event however each driver spells it.
    """
    return (time.monotonic() - started) * 1000 >= timeout_ms() * 0.9


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

# Bounded on purpose. A warehouse can have tens of thousands of columns and the
# whole point of this payload is to be small enough to sit in the editor's
# autocomplete without being fetched again.
SCHEMA_TABLE_LIMIT = 300
SCHEMA_COLUMN_LIMIT = 4000


def introspect(source):
    """List the tables and columns the source's role can see.

    Reads information_schema rather than the engine's own catalogue because
    information_schema already filters to what the connected role has
    privileges on -- so the autocomplete offers exactly what the query is
    allowed to touch, and does not advertise the existence of tables the role
    cannot read.
    """
    with _session(source) as (dialect, cursor):
        sql, params = dialect.schema_query(source)
        cursor.execute(sql, params)
        records = list(cursor.fetchall())
        default_schema = dialect.default_schema(source)

    tables = []
    index = {}
    for schema, table, column, data_type, table_type in records:
        key = (schema, table)
        entry = index.get(key)
        if entry is None:
            if len(tables) >= SCHEMA_TABLE_LIMIT:
                continue
            entry = {
                'schema': schema,
                'name': table,
                'qualified': table if schema == default_schema else schema + '.' + table,
                'kind': 'view' if table_type == 'VIEW' else 'table',
                'columns': [],
            }
            index[key] = entry
            tables.append(entry)
        entry['columns'].append({'name': column, 'type': data_type})
    return tables


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


def test_connection(source):
    """Open a connection, ask one harmless question, and report what happened.

    Returns a short description of what answered. Raises ConnectorError with a
    message an operator can act on when it did not.
    """
    with _session(source) as (dialect, cursor):
        return dialect.describe(cursor)
