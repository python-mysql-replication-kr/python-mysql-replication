import binascii
import struct
import datetime
import decimal
import zlib
import json

from pymysqlreplication.constants.STATUS_VAR_KEY import *
from pymysqlreplication.exceptions import StatusVariableMismatch
from typing import Union, Optional


class BinLogEvent(object):
    def __init__(
        self,
        from_packet,
        event_size,
        table_map,
        ctl_connection,
        mysql_version=(0, 0, 0),
        only_tables=None,
        ignored_tables=None,
        only_schemas=None,
        ignored_schemas=None,
        freeze_schema=False,
        ignore_decode_errors=False,
        verify_checksum=False,
        optional_meta_data=False,
    ):
        self.packet = from_packet
        self.table_map = table_map
        self.event_type = self.packet.event_type
        self.timestamp = self.packet.timestamp
        self.event_size = event_size
        self._ctl_connection = ctl_connection
        self.mysql_version = mysql_version
        self._ignore_decode_errors = ignore_decode_errors
        self._verify_checksum = verify_checksum
        self._is_event_valid = None
        # The event have been fully processed, if processed is false
        # the event will be skipped
        self._processed = True
        self.complete = True
        self.datetime = datetime.datetime.utcfromtimestamp(self.timestamp).isoformat()
        self._verify_event()

    def _read_table_id(self):
        # Table ID is 6 byte
        # pad little-endian number
        table_id = self.packet.read(6) + b"\x00\x00"
        return struct.unpack("<Q", table_id)[0]

    def _verify_event(self):
        if not self._verify_checksum:
            return

        self.packet.rewind(1)
        data = self.packet.read(19 + self.event_size)
        footer = self.packet.read(4)
        byte_data = zlib.crc32(data).to_bytes(4, byteorder="little")
        self._is_event_valid = True if byte_data == footer else False
        self.packet.read_bytes -= 19 + self.event_size + 4
        self.packet.rewind(20)

    def dump(self):
        print("=== %s ===" % (self.__class__.__name__))
        print("Date: %s" % (self.datetime))
        print("Log position: %d" % self.packet.log_pos)
        print("Event size: %d" % (self.event_size))
        print("Read bytes: %d" % (self.packet.read_bytes))
        self._dump()
        print()

    def _dump(self):
        """Core data dumped for the event"""
        pass

    def to_json(self):
        binlog_event_dict = {
            "evnet_type": self.event_type,
            "date": self.datetime,
            "log_pos": self.packet.log_pos,
            "event_size": self.event_size,
            "read_bytes": self.packet.read_bytes,
        }
        binlog_event_dict.update(self._to_json())

        return json.dumps(binlog_event_dict)

    def _to_json(self):
        """Core data change to json for the event"""
        pass


class GtidEvent(BinLogEvent):
    """
    GTID change in binlog event

    For more information: `[GTID] <https://mariadb.com/kb/en/gtid/>`_ `[see also] <https://dev.mysql.com/doc/dev/mysql-server/latest/classbinary__log_1_1Gtid__event.html>`_

    :ivar commit_flag: 1byte - 00000001 = Transaction may have changes logged with SBR.
            In 5.6, 5.7.0-5.7.18, and 8.0.0-8.0.1, this flag is always set. Starting in 5.7.19 and 8.0.2, this flag is cleared if the transaction only contains row events. It is set if any part of the transaction is written in statement format.
    :ivar sid: 16 byte sequence - UUID representing the SID
    :ivar gno: int - Group number, second component of GTID.
    :ivar lt_type: int(1 byte) - The type of logical timestamp used in the logical clock fields.
    :ivar last_committed: Store the transaction's commit parent sequence_number
    :ivar sequence_number: The transaction's logical timestamp assigned at prepare phase
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        self.commit_flag = struct.unpack("!B", self.packet.read(1))[0] == 1
        self.sid = self.packet.read(16)
        self.gno = struct.unpack("<Q", self.packet.read(8))[0]
        self.lt_type = self.packet.read(1)[0]

        if self.mysql_version >= (5, 7):
            self.last_committed = struct.unpack("<Q", self.packet.read(8))[0]
            self.sequence_number = struct.unpack("<Q", self.packet.read(8))[0]

    @property
    def gtid(self):
        """
        GTID = source_id:transaction_id
        Eg: 3E11FA47-71CA-11E1-9E33-C80AA9429562:23
        See: http://dev.mysql.com/doc/refman/5.6/en/replication-gtids-concepts.html"""
        nibbles = binascii.hexlify(self.sid).decode("ascii")
        gtid = "%s-%s-%s-%s-%s:%d" % (
            nibbles[:8],
            nibbles[8:12],
            nibbles[12:16],
            nibbles[16:20],
            nibbles[20:],
            self.gno,
        )
        return gtid

    def _dump(self):
        print("Commit: %s" % self.commit_flag)
        print("GTID_NEXT: %s" % self.gtid)
        if hasattr(self, "last_committed"):
            print("last_committed: %d" % self.last_committed)
            print("sequence_number: %d" % self.sequence_number)

    def _to_json(self):
        gtid_event_dict = {
            "commit": self.commit_flag,
            "gtid_next": self.gtid,
        }
        if hasattr(self, "last_committed"):
            gtid_event_dict["last_committed"] = self.last_committed
            gtid_event_dict["sequence_number"] = self.sequence_number
        return gtid_event_dict

    def __repr__(self):
        return '<GtidEvent "%s">' % self.gtid


class PreviousGtidsEvent(BinLogEvent):
    """
    PreviousGtidEvent is contains the Gtids executed in the last binary log file.
    Attributes:
        n_sid: which size is the gtid_set
        sid: 16bytes UUID as a binary
        n_intervals: how many intervals are sent
    Eg: [4c9e3dfc-9d25-11e9-8d2e-0242ac1cfd7e:1-100, 4c9e3dfc-9d25-11e9-8d2e-0242ac1cfd7e:1-10:20-30]
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(PreviousGtidsEvent, self).__init__(
            from_packet, event_size, table_map, ctl_connection, **kwargs
        )

        self._n_sid = self.packet.read_int64()
        self._gtids = []

        for _ in range(self._n_sid):
            sid = self.packet.read(16)
            n_intervals = self.packet.read_uint64()
            intervals = [
                f"{self.packet.read_int64()}-{self.packet.read_uint64()}"
                for _ in range(n_intervals)
            ]
            nibbles = binascii.hexlify(sid).decode("ascii")
            gtid = "%s-%s-%s-%s-%s:%s" % (
                nibbles[:8],
                nibbles[8:12],
                nibbles[12:16],
                nibbles[16:20],
                nibbles[20:],
                ":".join(intervals),
            )
            self._gtids.append(gtid)

        self._previous_gtids = ",".join(self._gtids)

    def _dump(self):
        print("previous_gtids: %s" % self._previous_gtids)

    def _to_json(self):
        return {"previous_gtids": self._previous_gtids}

    def __repr__(self):
        return '<PreviousGtidsEvent "%s">' % self._previous_gtids


class MariadbGtidEvent(BinLogEvent):
    """
    GTID(Global Transaction Identifier) change in binlog event in MariaDB

    For more information: `[see details] <https://mariadb.com/kb/en/gtid_event/>`_.

    :ivar server_id: int - The ID of the server where the GTID event occurred.
    :ivar gtid_seq_no: int - The sequence number of the GTID event.
    :ivar domain_id: int - The domain ID associated with the GTID event.
    :ivar flags: int - Flags related to the GTID event.
    :ivar gtid: str - The Global Transaction Identifier in the format ‘domain_id-server_id-gtid_seq_no’.
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        self.server_id = self.packet.server_id
        self.gtid_seq_no = self.packet.read_uint64()
        self.domain_id = self.packet.read_uint32()
        self.flags = self.packet.read_uint8()
        self.gtid = "%d-%d-%d" % (self.domain_id, self.server_id, self.gtid_seq_no)

    def _dump(self):
        super()._dump()
        print("Flags:", self.flags)
        print("GTID:", self.gtid)

    def _to_json(self):
        return {"flags": self.flags, "gtid": self.gtid}


class MariadbBinLogCheckPointEvent(BinLogEvent):
    """
    Represents a checkpoint in a binlog event in MariaDB.

    More details are available in the MariaDB Knowledge Base:
    https://mariadb.com/kb/en/binlog_checkpoint_event/

    :ivar filename_length: int - The length of the filename.
    :ivar filename: str - The name of the file saved at the checkpoint.
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(MariadbBinLogCheckPointEvent, self).__init__(
            from_packet, event_size, table_map, ctl_connection, **kwargs
        )
        filename_length = self.packet.read_uint32()
        self.filename = self.packet.read(filename_length).decode()

    def _dump(self):
        print("Filename:", self.filename)

    def _to_json(self):
        return {"Filename": self.filename}


class MariadbAnnotateRowsEvent(BinLogEvent):
    """
    Annotate rows event
    If you want to check this binlog, change the value of the flag(line 382 of the 'binlogstream.py') option to 2
    https://mariadb.com/kb/en/annotate_rows_event/

    :ivar sql_statement: str - The SQL statement
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.sql_statement = self.packet.read(event_size)

    def _dump(self):
        super()._dump()
        print("SQL statement :", self.sql_statement)

    def _to_json(self):
        return {"sql_statement": self.sql_statement}


class MariadbGtidListEvent(BinLogEvent):
    """
    GTID List event
    https://mariadb.com/kb/en/gtid_list_event/

    :ivar gtid_length: int - Number of GTIDs
    :ivar gtid_list: list - list of 'MariadbGtidObejct'

    'MariadbGtidObejct' Attributes:
        domain_id: Replication Domain ID
        server_id: Server_ID
        gtid_seq_no: GTID sequence
        gtid: 'domain_id'+ 'server_id' + 'gtid_seq_no'
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(MariadbGtidListEvent, self).__init__(
            from_packet, event_size, table_map, ctl_connection, **kwargs
        )

        class MariadbGtidObejct(BinLogEvent):
            """
            Information class of elements in GTID list
            """

            def __init__(
                self, from_packet, event_size, table_map, ctl_connection, **kwargs
            ):
                super(MariadbGtidObejct, self).__init__(
                    from_packet, event_size, table_map, ctl_connection, **kwargs
                )
                self.domain_id = self.packet.read_uint32()
                self.server_id = self.packet.read_uint32()
                self.gtid_seq_no = self.packet.read_uint64()
                self.gtid = "%d-%d-%d" % (
                    self.domain_id,
                    self.server_id,
                    self.gtid_seq_no,
                )

        self.gtid_length = self.packet.read_uint32()
        self.gtid_list = [
            MariadbGtidObejct(
                from_packet, event_size, table_map, ctl_connection, **kwargs
            )
            for i in range(self.gtid_length)
        ]

    def _to_json(self):
        return {"gtid_length": self.gtid_length, "gtid_list": self.gtid_list}


class RotateEvent(BinLogEvent):
    """
    Change MySQL bin log file
    Represents information for the slave to know the name of the binary log it is going to receive.

    For more information: `[see details] <https://dev.mysql.com/doc/dev/mysql-server/latest/classbinary__log_1_1Rotate__event.html>`_.

    :ivar position: int - Position inside next binlog
    :ivar next_binlog: str - Name of next binlog file
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.position = struct.unpack("<Q", self.packet.read(8))[0]
        self.next_binlog = self.packet.read(event_size - 8).decode()

    def dump(self):
        print("=== %s ===" % (self.__class__.__name__))
        print("Position: %d" % self.position)
        print("Next binlog file: %s" % self.next_binlog)
        print()

    def to_json(self):
        return {"position": self.position, "next_binlog": self.next_binlog}


class XAPrepareEvent(BinLogEvent):
    """
    An XA prepare event is generated for a XA prepared transaction.
    Like Xid_event, it contains XID of the **prepared** transaction.

    For more information: `[see details] <https://dev.mysql.com/doc/refman/8.0/en/xa-statements.html>`_.

    :ivar one_phase: current XA transaction commit method
    :ivar xid_format_id: a number that identifies the format used by the gtrid and bqual values
    :ivar xid: serialized XID representation of XA transaction (xid_gtrid + xid_bqual)
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # one_phase is True: XA COMMIT ... ONE PHASE
        # one_phase is False: XA PREPARE
        self.one_phase = self.packet.read(1) != b"\x00"
        self.xid_format_id = struct.unpack("<I", self.packet.read(4))[0]
        gtrid_length = struct.unpack("<I", self.packet.read(4))[0]
        bqual_length = struct.unpack("<I", self.packet.read(4))[0]
        self.xid_gtrid = self.packet.read(gtrid_length)
        self.xid_bqual = self.packet.read(bqual_length)

    @property
    def xid(self):
        return self.xid_gtrid.decode() + self.xid_bqual.decode()

    def _dump(self):
        print("One phase: %s" % self.one_phase)
        print("XID formatID: %d" % self.xid_format_id)
        print("XID: %s" % self.xid)

    def _to_json(self):
        return {
            "one_phase": self.one_phase,
            "xid_format_id": self.xid_format_id,
            "xid": self.xid,
        }


class FormatDescriptionEvent(BinLogEvent):
    """
    Represents a Format Description Event in the MySQL binary log.

    This event is written at the start of a binary log file for binlog version 4.
    It provides the necessary information to decode subsequent events in the file.

    :ivar binlog_version: int - Version of the binary log format.
    :ivar mysql_version_str: str - Server's MySQL version in string format.
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.binlog_version = struct.unpack("<H", self.packet.read(2))
        self.mysql_version_str = self.packet.read(50).rstrip(b"\0").decode()
        numbers = self.mysql_version_str.split("-")[0]
        self.mysql_version = tuple(map(int, numbers.split(".")))

    def _dump(self):
        print("Binlog version: %s" % self.binlog_version)
        print("MySQL version: %s" % self.mysql_version_str)

    def _to_json(self):
        return {
            "binlog_version": self.binlog_version,
            "mysql_version_str": self.mysql_version_str,
        }


class StopEvent(BinLogEvent):
    pass


class XidEvent(BinLogEvent):
    """
    A COMMIT event generated when COMMIT of a transaction that modifies one or more tables of an XA-capable storage engine occurs.

    For more information: `[see details] <https://mariadb.com/kb/en/xid_event/>`_.

    :ivar xid: uint - Transaction ID for 2 Phase Commit.
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.xid = struct.unpack("<Q", self.packet.read(8))[0]

    def _dump(self):
        super()._dump()
        print("Transaction ID: %d" % (self.xid))

    def _to_json(self):
        return {"transaction_id": self.xid}


class HeartbeatLogEvent(BinLogEvent):
    """A Heartbeat event
    Heartbeats are sent by the master.
    Master sends heartbeats when there are no unsent events in the binary log file after certain period of time.
    The interval is defined by MASTER_HEARTBEAT_PERIOD connection setting.

    `[see MASTER_HEARTBEAT_PERIOD] <https://dev.mysql.com/doc/refman/8.0/en/change-master-to.html>`_.

    A Mysql server also does it for each skipped events in the log.
    This is because to make the slave bump its position so that
    if a disconnection occurs, the slave will only reconnects from the lasted skipped position. (Baloo's idea)

    (see Binlog_sender::send_events in sql/rpl_binlog_sender.cc)

    Warning:
    That makes 106 bytes of data for skipped event in the binlog.
    *this is also the case with GTID replication*.
    To mitigate such behavior, you are expected to keep the binlog small
    (see max_binlog_size, defaults to 1G).
    In any case, the timestamp is 0 (as in 1970-01-01T00:00:00).

    :ivar ident: Name of the current binlog
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.ident = self.packet.read(event_size).decode()

    def _dump(self):
        super()._dump()
        print("Current binlog: %s" % (self.ident))

    def _to_json(self):
        return {"current_binlog": self.ident}


class QueryEvent(BinLogEvent):
    """
    QueryEvent is generated for each query that modified database.
    If row-based replication is used, DML will not be logged as RowsEvent instead.

    :ivar slave_proxy_id: int - The id of the thread that issued this statement on the master server
    :ivar execution_time: int - The time from when the query started to when it was logged in the binlog, in seconds.
    :ivar schema_length: int - The length of the name of the currently selected database.
    :ivar error_code: int - Error code generated by the master
    :ivar status_vars_length: int - The length of the status variable

    :ivar schema: str - The name of the currently selected database.
    :ivar query: str - The query executed.
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # Post-header
        self.slave_proxy_id = self.packet.read_uint32()
        self.execution_time = self.packet.read_uint32()
        self.schema_length = struct.unpack("!B", self.packet.read(1))[0]
        self.error_code = self.packet.read_uint16()
        self.status_vars_length = self.packet.read_uint16()

        # Payload
        status_vars_end_pos = self.packet.read_bytes + self.status_vars_length
        while self.packet.read_bytes < status_vars_end_pos:
            # read KEY for status variable
            status_vars_key = self.packet.read_uint8()
            # read VALUE for status variable
            self._read_status_vars_value_for_key(status_vars_key)

        self.schema = self.packet.read(self.schema_length)
        self.packet.advance(1)
        query = self.packet.read(
            event_size - 13 - self.status_vars_length - self.schema_length - 1
        )
        self.query = query.decode("utf-8", errors="backslashreplace")
        # string[EOF]    query

    def _dump(self):
        super()._dump()
        print("Schema: %s" % (self.schema))
        print("Execution time: %d" % (self.execution_time))
        print("Query: %s" % (self.query))

    def _to_json(self):
        return {
            "schema": self.schema.decode("utf-8"),
            "execution_time": self.execution_time,
            "query": self.query,
        }

    def _read_status_vars_value_for_key(self, key):
        """parse status variable VALUE for given KEY

        A status variable in query events is a sequence of status KEY-VALUE pairs.
        Parsing logic from mysql-server source code edited by dongwook-chan
        https://github.com/mysql/mysql-server/blob/beb865a960b9a8a16cf999c323e46c5b0c67f21f/libbinlogevents/src/statement_events.cpp#L181-L336

        :ivar key: key for status variable
        """
        if key == Q_FLAGS2_CODE:  # 0x00
            self.flags2 = self.packet.read_uint32()
        elif key == Q_SQL_MODE_CODE:  # 0x01
            self.sql_mode = self.packet.read_uint64()
        elif key == Q_CATALOG_CODE:  # 0x02 for MySQL 5.0.x
            pass
        elif key == Q_AUTO_INCREMENT:  # 0x03
            self.auto_increment_increment = self.packet.read_uint16()
            self.auto_increment_offset = self.packet.read_uint16()
        elif key == Q_CHARSET_CODE:  # 0x04
            self.character_set_client = self.packet.read_uint16()
            self.collation_connection = self.packet.read_uint16()
            self.collation_server = self.packet.read_uint16()
        elif key == Q_TIME_ZONE_CODE:  # 0x05
            time_zone_len = self.packet.read_uint8()
            if time_zone_len:
                self.time_zone = self.packet.read(time_zone_len)
        elif key == Q_CATALOG_NZ_CODE:  # 0x06
            catalog_len = self.packet.read_uint8()
            if catalog_len:
                self.catalog_nz_code = self.packet.read(catalog_len)
        elif key == Q_LC_TIME_NAMES_CODE:  # 0x07
            self.lc_time_names_number = self.packet.read_uint16()
        elif key == Q_CHARSET_DATABASE_CODE:  # 0x08
            self.charset_database_number = self.packet.read_uint16()
        elif key == Q_TABLE_MAP_FOR_UPDATE_CODE:  # 0x09
            self.table_map_for_update = self.packet.read_uint64()
        elif key == Q_MASTER_DATA_WRITTEN_CODE:  # 0x0A
            pass
        elif key == Q_INVOKER:  # 0x0B
            user_len = self.packet.read_uint8()
            if user_len:
                self.user = self.packet.read(user_len)
            host_len = self.packet.read_uint8()
            if host_len:
                self.host = self.packet.read(host_len)
        elif key == Q_UPDATED_DB_NAMES:  # 0x0C
            mts_accessed_dbs = self.packet.read_uint8()
            """
            mts_accessed_dbs < 254:
                `mts_accessed_dbs` is equal to the number of dbs
                accessed by the query event.
            mts_accessed_dbs == 254:
                This is the case where the number of dbs accessed
                is 1 and the name of the only db is ""
                Since no further parsing required(empty name), return.
            """
            if mts_accessed_dbs == 254:
                return
            dbs = []
            for i in range(mts_accessed_dbs):
                db = self.packet.read_string()
                dbs.append(db)
            self.mts_accessed_db_names = dbs
        elif key == Q_MICROSECONDS:  # 0x0D
            self.microseconds = self.packet.read_uint24()
        elif key == Q_COMMIT_TS:  # 0x0E
            pass
        elif key == Q_COMMIT_TS2:  # 0x0F
            pass
        elif key == Q_EXPLICIT_DEFAULTS_FOR_TIMESTAMP:  # 0x10
            self.explicit_defaults_ts = self.packet.read_uint8()
        elif key == Q_DDL_LOGGED_WITH_XID:  # 0x11
            self.ddl_xid = self.packet.read_uint64()
        elif key == Q_DEFAULT_COLLATION_FOR_UTF8MB4:  # 0x12
            self.default_collation_for_utf8mb4_number = self.packet.read_uint16()
        elif key == Q_SQL_REQUIRE_PRIMARY_KEY:  # 0x13
            self.sql_require_primary_key = self.packet.read_uint8()
        elif key == Q_DEFAULT_TABLE_ENCRYPTION:  # 0x14
            self.default_table_encryption = self.packet.read_uint8()
        elif key == Q_HRNOW:
            self.hrnow = self.packet.read_uint24()
        elif key == Q_XID:
            self.xid = self.packet.read_uint64()
        else:
            raise StatusVariableMismatch


class BeginLoadQueryEvent(BinLogEvent):
    """
    This event is written into the binary log file for LOAD DATA INFILE events
    if the server variable binlog_mode was set to "STATEMENT".

    :ivar file_id: the id of the file
    :ivar block-data: data block about "LOAD DATA INFILE"
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # Payload
        self.file_id = self.packet.read_uint32()
        self.block_data = self.packet.read(event_size - 4)

    def _dump(self):
        super()._dump()
        print("File id: %d" % (self.file_id))
        print("Block data: %s" % (self.block_data))

    def _to_json(self):
        return {"file_id": self.file_id, "block_data": self.block_data}


class ExecuteLoadQueryEvent(BinLogEvent):
    """
    This event handles "LOAD DATA INFILE" statement.
    LOAD DATA INFILE statement reads data from file and insert into database's table.
    Since QueryEvent cannot explain this special action, ExecuteLoadQueryEvent is needed.
    So it is similar to a QUERY_EVENT except that it has extra static fields.

    :ivar slave_proxy_id: int - The id of the thread that issued this statement on the master server
    :ivar execution_time: int - The number of seconds that the statement took to execute
    :ivar schema_length: int - The length of the default database's name when the statement was executed.
    :ivar error_code: int - The error code resulting from execution of the statement on the master
    :ivar status_vars_length: int - The length of the status variable block
    :ivar file_id: int - The id of the loaded file
    :ivar start_pos: int - Offset from the start of the statement to the beginning of the filename
    :ivar end_pos: int - Offset from the start of the statement to the end of the filename
    :ivar dup_handling_flags: int - How LOAD DATA INFILE handles duplicated data (0x0: error, 0x1: ignore, 0x2: replace)
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # Post-header
        self.slave_proxy_id = self.packet.read_uint32()
        self.execution_time = self.packet.read_uint32()
        self.schema_length = self.packet.read_uint8()
        self.error_code = self.packet.read_uint16()
        self.status_vars_length = self.packet.read_uint16()

        # Payload
        self.file_id = self.packet.read_uint32()
        self.start_pos = self.packet.read_uint32()
        self.end_pos = self.packet.read_uint32()
        self.dup_handling_flags = self.packet.read_uint8()

    def _dump(self):
        super(ExecuteLoadQueryEvent, self)._dump()
        print("Slave proxy id: %d" % (self.slave_proxy_id))
        print("Execution time: %d" % (self.execution_time))
        print("Schema length: %d" % (self.schema_length))
        print("Error code: %d" % (self.error_code))
        print("Status vars length: %d" % (self.status_vars_length))
        print("File id: %d" % (self.file_id))
        print("Start pos: %d" % (self.start_pos))
        print("End pos: %d" % (self.end_pos))
        print("Dup handling flags: %d" % (self.dup_handling_flags))

    def _to_json(self):
        return {
            "slave_proxy_id": self.slave_proxy_id,
            "execution_time": self.execution_time,
            "schema_length": self.schema_length,
            "error_code": self.error_code,
            "status_vars_length": self.status_vars_length,
            "file_id": self.file_id,
            "start_pos": self.start_pos,
            "end_pos": self.end_pos,
            "dup_handling_flags": self.dup_handling_flags,
        }


class IntvarEvent(BinLogEvent):
    """
    Stores the value of auto-increment variables.
    This event will be created just before a QueryEvent.

    :ivar type: int - 1 byte identifying the type of variable stored.
    Can be either LAST_INSERT_ID_EVENT (1) or INSERT_ID_EVENT (2).
    :ivar value: int - The value of the variable
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # Payload
        self.type = self.packet.read_uint8()
        self.value = self.packet.read_uint32()

    def _dump(self):
        super()._dump()
        print("type: %d" % (self.type))
        print("Value: %d" % (self.value))

    def _to_json(self):
        return {"type": self.type, "value": self.value}


class RandEvent(BinLogEvent):
    """
    RandEvent is generated every time a statement uses the RAND() function.
    Indicates the seed values to use for generating a random number with RAND() in the next statement.

    RandEvent only works in statement-based logging (need to set binlog_format as 'STATEMENT')
    and only works when the seed number is not specified.

    :ivar seed1: int - value for the first seed
    :ivar seed2: int - value for the second seed
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        # Payload
        self._seed1 = self.packet.read_uint64()
        self._seed2 = self.packet.read_uint64()

    @property
    def seed1(self):
        """Get the first seed value"""
        return self._seed1

    @property
    def seed2(self):
        """Get the second seed value"""
        return self._seed2

    def _dump(self):
        super()._dump()
        print("seed1: %d" % (self.seed1))
        print("seed2: %d" % (self.seed2))

    def _to_json(self):
        return {"seed1": self.seed1, "seed2": self.seed2}


class UserVarEvent(BinLogEvent):
    """
    UserVarEvent is generated every time a statement uses a user variable.
    Indicates the value to use for the user variable in the next statement.

    :ivar name_len: int - Length of user variable
    :ivar name: str - User variable name
    :ivar value: str - Value of the user variable
    :ivar type: int - Type of the user variable
    :ivar charset: int - The number of the character set for the user variable
    :ivar is_null: int - Non-zero if the variable value is the SQL NULL value, 0 otherwise
    :ivar flags: int - Extra flags associated with the user variable
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(UserVarEvent, self).__init__(
            from_packet, event_size, table_map, ctl_connection, **kwargs
        )

        # Payload
        self.name_len: int = self.packet.read_uint32()
        self.name: str = self.packet.read(self.name_len).decode()
        self.is_null: int = self.packet.read_uint8()
        self.type_to_codes_and_method: dict = {
            0x00: ["STRING_RESULT", self._read_string],
            0x01: ["REAL_RESULT", self._read_real],
            0x02: ["INT_RESULT", self._read_int],
            0x03: ["ROW_RESULT", self._read_default],
            0x04: ["DECIMAL_RESULT", self._read_decimal],
        }

        self.value: Optional[Union[str, float, int, decimal.Decimal]] = None
        self.flags: Optional[int] = None
        self.temp_value_buffer: Union[bytes, memoryview] = b""

        if not self.is_null:
            self.type: int = self.packet.read_uint8()
            self.charset: int = self.packet.read_uint32()
            self.value_len: int = self.packet.read_uint32()
            self.temp_value_buffer: Union[bytes, memoryview] = self.packet.read(
                self.value_len
            )
            self.flags: int = self.packet.read_uint8()
            self._set_value_from_temp_buffer()
        else:
            self.type, self.charset, self.value_len, self.value, self.flags = (
                None,
                None,
                None,
                None,
                None,
            )

    def _set_value_from_temp_buffer(self):
        """
        Set the value from the temporary buffer based on the type code.
        """
        if self.temp_value_buffer:
            type_code, read_method = self.type_to_codes_and_method.get(
                self.type, ["UNKNOWN_RESULT", self._read_default]
            )
            if type_code == "INT_RESULT":
                self.value = read_method(self.temp_value_buffer, self.flags)
            else:
                self.value = read_method(self.temp_value_buffer)

    def _read_string(self, buffer: bytes) -> str:
        """
        Read string data.
        """
        return buffer.decode()

    def _read_real(self, buffer: bytes) -> float:
        """
        Read real data.
        """
        return struct.unpack("<d", buffer)[0]

    def _read_int(self, buffer: bytes, flags: int) -> int:
        """
        Read integer data.
        """
        fmt = "<Q" if flags == 1 else "<q"
        return struct.unpack(fmt, buffer)[0]

    def _read_decimal(self, buffer: bytes) -> decimal.Decimal:
        """
        Read decimal data.
        """
        self.precision = self.temp_value_buffer[0]
        self.decimals = self.temp_value_buffer[1]
        raw_decimal = self.temp_value_buffer[2:]
        return self._parse_decimal_from_bytes(
            raw_decimal, self.precision, self.decimals
        )

    def _read_default(self) -> bytes:
        """
        Read default data.
        Used when the type is None.
        """
        return self.packet.read(self.value_len)

    @staticmethod
    def _parse_decimal_from_bytes(
        raw_decimal: bytes, precision: int, decimals: int
    ) -> decimal.Decimal:
        """
        Parse decimal from bytes.
        """
        digits_per_integer = 9
        compressed_bytes = [0, 1, 1, 2, 2, 3, 3, 4, 4, 4]
        integral = precision - decimals

        uncomp_integral, comp_integral = divmod(integral, digits_per_integer)
        uncomp_fractional, comp_fractional = divmod(decimals, digits_per_integer)

        res = "-" if not raw_decimal[0] & 0x80 else ""
        mask = -1 if res == "-" else 0
        raw_decimal = bytearray([raw_decimal[0] ^ 0x80]) + raw_decimal[1:]

        def decode_decimal_decompress_value(comp_indx, data, mask):
            size = compressed_bytes[comp_indx]
            if size > 0:
                databuff = bytearray(data[:size])
                for i in range(size):
                    databuff[i] = (databuff[i] ^ mask) & 0xFF
                return size, int.from_bytes(databuff, byteorder="big")
            return 0, 0

        pointer, value = decode_decimal_decompress_value(
            comp_integral, raw_decimal, mask
        )
        res += str(value)

        for _ in range(uncomp_integral):
            value = struct.unpack(">i", raw_decimal[pointer : pointer + 4])[0] ^ mask
            res += "%09d" % value
            pointer += 4

        res += "."

        for _ in range(uncomp_fractional):
            value = struct.unpack(">i", raw_decimal[pointer : pointer + 4])[0] ^ mask
            res += "%09d" % value
            pointer += 4

        size, value = decode_decimal_decompress_value(
            comp_fractional, raw_decimal[pointer:], mask
        )
        if size > 0:
            res += "%0*d" % (comp_fractional, value)
        return decimal.Decimal(res)

    def _dump(self) -> None:
        super(UserVarEvent, self)._dump()
        print("User variable name: %s" % self.name)
        print("Is NULL: %s" % ("Yes" if self.is_null else "No"))
        if not self.is_null:
            print(
                "Type: %s"
                % self.type_to_codes_and_method.get(self.type, ["UNKNOWN_TYPE"])[0]
            )
            print("Charset: %s" % self.charset)
            print("Value: %s" % self.value)
            print("Flags: %s" % self.flags)

    def _to_json(self):
        user_var_event_dict = {
            "user_variable_name": self.name,
        }
        if not self.is_null:
            user_var_event_dict["type"] = self.type_to_codes_and_method.get(
                self.type, ["UNKNOWN_TYPE"]
            )[0]
            user_var_event_dict["charset"] = self.charset
            user_var_event_dict["value"] = self.value
            user_var_event_dict["flags"] = self.flags

        return user_var_event_dict


class MariadbStartEncryptionEvent(BinLogEvent):
    """
    Since MariaDB 10.1.7,
    the START_ENCRYPTION event is written to every binary log file
    if encrypt_binlog is set to ON. Prior to enabling this setting,
    additional configuration steps are required in MariaDB.
    (Link: https://mariadb.com/kb/en/encrypting-binary-logs/)

    This event is written just once, after the Format Description event

    Attributes:
        schema: The Encryption scheme, always set to 1 for system files.
        key_version: The Encryption key version.
        nonce: Nonce (12 random bytes) of current binlog file.
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        self.schema = self.packet.read_uint8()
        self.key_version = self.packet.read_uint32()
        self.nonce = self.packet.read(12)

    def _dump(self):
        print("Schema: %d" % self.schema)
        print("Key version: %d" % self.key_version)
        print(f"Nonce: {self.nonce}")

    def _to_json(self):
        return {
            "schema": self.schema,
            "key_version": self.key_version,
            "nonce": self.nonce,
        }


class RowsQueryLogEvent(BinLogEvent):
    """
    Record original query for the row events in Row-Based Replication

    More details are available in the MySQL Knowledge Base:
    https://dev.mysql.com/doc/dev/mysql-server/latest/classRows__query__log__event.html

    :ivar query_length: uint - Length of the SQL statement
    :ivar query: str - The executed SQL statement
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(RowsQueryLogEvent, self).__init__(
            from_packet, event_size, table_map, ctl_connection, **kwargs
        )
        self.query_length = self.packet.read_uint8()
        self.query = self.packet.read(self.query_length).decode("utf-8")

    def _dump(self):
        print("=== %s ===" % (self.__class__.__name__))
        print("Query length: %d" % self.query_length)
        print("Query: %s" % self.query)

    def _to_json(self):
        return {"query_length": self.query_length, "query": self.query}


class NotImplementedEvent(BinLogEvent):
    """
    Used as a temporary class for events that have not yet been implemented.

    The event referencing this class skips parsing.
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.packet.advance(event_size)
