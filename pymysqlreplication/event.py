# -*- coding: utf-8 -*-

import binascii
import struct
import datetime
import pymysql
from pymysql.protocol import MysqlPacket
from typing import Dict, List, NoReturn, Tuple, Type, Union
from pymysqlreplication.constants.STATUS_VAR_KEY import *
from pymysqlreplication.exceptions import StatusVariableMismatch
from pymysqlreplication.table import Table


class BinLogEvent(object):
    def __init__(self, from_packet, event_size, table_map, ctl_connection,
                 mysql_version=(0,0,0),
                 only_tables=None,
                 ignored_tables=None,
                 only_schemas=None,
                 ignored_schemas=None,
                 freeze_schema=False,
                 fail_on_table_metadata_unavailable=False,
                 ignore_decode_errors=False):
        self.packet: MysqlPacket = from_packet
        self.table_map: Dict[int, Table] = table_map
        self.event_type: int = self.packet.event_type
        self.timestamp: int = self.packet.timestamp
        self.event_size: int = event_size
        self._ctl_connection: pymysql.connections.Connection = ctl_connection
        self.mysql_version: Tuple[int, int ,int] = mysql_version
        self._fail_on_table_metadata_unavailable: bool = fail_on_table_metadata_unavailable
        self._ignore_decode_errors: bool = ignore_decode_errors
        # The event have been fully processed, if processed is false
        # the event will be skipped
        self._processed: bool = True
        self.complete: bool = True

    def _read_table_id(self):
        # Table ID is 6 byte
        # pad little-endian number
        table_id = self.packet.read(6) + b"\x00\x00"
        return struct.unpack('<Q', table_id)[0]

    def dump(self):
        print("=== %s ===" % (self.__class__.__name__))
        print("Date: %s" % (datetime.datetime.utcfromtimestamp(self.timestamp)
                            .isoformat()))
        print("Log position: %d" % self.packet.log_pos)
        print("Event size: %d" % (self.event_size))
        print("Read bytes: %d" % (self.packet.read_bytes))
        self._dump()
        print()

    def _dump(self):
        """Core data dumped for the event"""
        pass


class GtidEvent(BinLogEvent):
    """GTID change in binlog event

    For more information : `[see GtidEvent] <https://mariadb.com/kb/en/gtid/>`_ `[see also] <https://dev.mysql.com/doc/dev/mysql-server/latest/classbinary__log_1_1Gtid__event.html>`_ 

    :ivar commit_flag: 1byte - 00000001 = Transaction may have changes logged with SBR.
            In 5.6, 5.7.0-5.7.18, and 8.0.0-8.0.1, this flag is always set. Starting in 5.7.19 and 8.0.2, this flag is cleared if the transaction only contains row events. It is set if any part of the transaction is written in statement format.
    :ivar sid: 16 byte sequence - UUID representing the SID
    :ivar gno: int - Group number, second component of GTID.
    :ivar lt_type: int(1 byte) - The type of logical timestamp used in the logical clock fields.
    :ivar last_committed: Store the transaction's commit parent sequence_number
    :ivar sequence_number: The transaction's logical timestamp assigned at prepare phase
    """
    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map,
                                          ctl_connection, **kwargs)

        self.commit_flag: bool = struct.unpack("!B", self.packet.read(1))[0] == 1
        self.sid: bytes = self.packet.read(16)
        self.gno: int = struct.unpack('<Q', self.packet.read(8))[0]
        self.lt_type:int = self.packet.read(1)[0]

        if self.mysql_version >= (5, 7):
            self.last_committed: int = struct.unpack('<Q', self.packet.read(8))[0]
            self.sequence_number: int = struct.unpack('<Q', self.packet.read(8))[0]

    @property
    def gtid(self):
        """GTID = source_id:transaction_id
        Eg: 3E11FA47-71CA-11E1-9E33-C80AA9429562:23
        See: http://dev.mysql.com/doc/refman/5.6/en/replication-gtids-concepts.html"""
        nibbles = binascii.hexlify(self.sid).decode('ascii')
        gtid = '%s-%s-%s-%s-%s:%d' % (
            nibbles[:8], nibbles[8:12], nibbles[12:16], nibbles[16:20], nibbles[20:], self.gno
        )
        return gtid

    def _dump(self):
        print("Commit: %s" % self.commit_flag)
        print("GTID_NEXT: %s" % self.gtid)
        if hasattr(self, "last_committed"):
            print("last_committed: %d" % self.last_committed)
            print("sequence_number: %d" % self.sequence_number)

    def __repr__(self):
        return '<GtidEvent "%s">' % self.gtid


class MariadbGtidEvent(BinLogEvent):
    """
    GTID(Global Transaction Identifier) change in binlog event in MariaDB

    for more information: `[see MariadbGtidEvent] <https://mariadb.com/kb/en/gtid_event/>`_.

    :ivar server_id: int - The ID of the server where the GTID event occurred.
    :ivar gtid_seq_no: int - The sequence number of the GTID event.
    :ivar domain_id: int - The domain ID associated with the GTID event.
    :ivar flags: int - Flags related to the GTID event.
    :ivar gtid: str - The Global Transaction Identifier in the format ‘domain_id-server_id-gtid_seq_no’.
    """
    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):

        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        self.server_id: int = self.packet.server_id
        self.gtid_seq_no: int = self.packet.read_uint64()
        self.domain_id: int = self.packet.read_uint32()
        self.flags: int = self.packet.read_uint8()
        self.gtid: str = "%d-%d-%d" % (self.domain_id, self.server_id, self.gtid_seq_no)

    def _dump(self):
        super()._dump()
        print("Flags:", self.flags)
        print('GTID:', self.gtid)

class MariadbBinLogCheckPointEvent(BinLogEvent):
    """
    Represents a checkpoint in a binlog event in MariaDB.

    for more information: `[see MaridbBinLogCheckPointEvent] <https://mariadb.com/kb/en/binlog_checkpoint_event/>`_

    :ivar filename_length: int - The length of the filename.
    :ivar filename: str - The name of the file saved at the checkpoint.
    """

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(MariadbBinLogCheckPointEvent, self).__init__(from_packet, event_size, table_map, ctl_connection,
                                                           **kwargs)
        filename_length: int = self.packet.read_uint32()
        self.filename: str = self.packet.read(filename_length).decode()

    def _dump(self):
        print('Filename:', self.filename)

class MariadbAnnotateRowsEvent(BinLogEvent):
    """
    Annotate rows event 
    If you want to check this binlog, change the value of the flag(line 382 of the 'binlogstream.py') option to 2 
    https://mariadb.com/kb/en/annotate_rows_event/

    Attributes:
        sql_statement: str - The SQL statement
    """
    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.sql_statement: str = self.packet.read(event_size)

    def _dump(self):
        super()._dump()
        print("SQL statement :", self.sql_statement)

class MariadbGtidListEvent(BinLogEvent):
    """
    for more information: `[see MariadbGtidListEvent] <https://mariadb.com/kb/en/gtid_list_event/>`_

    :ivar gtid_length: int - Number of GTIDs
    :ivar gtid_list: List - list of 'MariadbGtidObejct'

    :ivar domain_id: int - Replication Domain ID (MariadbGtidObject)
    :ivar server_id: int -Server_ID (MariadbGtidObject)
    :ivar gtid_seq_no: int - GTID sequence (MariadbGtidObject)
    :ivar gtid: str - 'domain_id'+ 'server_id' + 'gtid_seq_no' (MariadbGtidObject)
    """
    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):

        super(MariadbGtidListEvent, self).__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        class MariadbGtidObejct(BinLogEvent):
            """
            Information class of elements in GTID list
            """
            def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
                super(MariadbGtidObejct, self).__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
                self.domain_id: int = self.packet.read_uint32()
                self.server_id: int = self.packet.read_uint32()
                self.gtid_seq_no: int = self.packet.read_uint64()
                self.gtid: str = "%d-%d-%d" % (self.domain_id, self.server_id, self.gtid_seq_no)


        self.gtid_length: int = self.packet.read_uint32()
        self.gtid_list: List[MariadbGtidObejct] = [MariadbGtidObejct(from_packet, event_size, table_map, ctl_connection, **kwargs) for i in range(self.gtid_length)]


class RotateEvent(BinLogEvent):
    """
    The event changes MySQL bin log file.
    It represents information for the slave to know the name of the binary log it is going to receive.

    For more information: `[see details] <https://dev.mysql.com/doc/dev/mysql-server/latest/classbinary__log_1_1Rotate__event.html>`_.

    In detail, the class creates the following python objects in the constructor:

    :ivar position: int - Position inside next binlog
    :ivar next_binlog: str - Name of next binlog file
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection,
                 **kwargs: str) -> None:
        super().__init__(from_packet, event_size, table_map,
                         ctl_connection, **kwargs)
        self.position: int = struct.unpack('<Q', self.packet.read(8))[0]
        self.next_binlog: str = self.packet.read(event_size - 8).decode()

    def dump(self) -> None:
        """Dumps the python objects for the event."""
        print("=== %s ===" % (self.__class__.__name__))
        print("Position: %d" % self.position)
        print("Next binlog file: %s" % self.next_binlog)
        print()


class XAPrepareEvent(BinLogEvent):
    """
    An XA prepare event is generated for a XA prepared transaction.
    Like Xid_event, it contains XID of the **prepared** transaction.

    For more information: `[see details] <https://dev.mysql.com/doc/refman/8.0/en/xa-statements.html>`_.

    In detail, the class creates the following python objects in the constructor:

    :ivar one_phase: str - current XA transaction commit method
    :ivar xid_format_id: int - a number that identifies the format used by the gtrid and bqual values
    :ivar xid: str - serialized XID representation of XA transaction (xid_gtrid + xid_bqual)
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection,
                 **kwargs: str) -> None:   
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # one_phase is True: XA COMMIT ... ONE PHASE
        # one_phase is False: XA PREPARE
        self.one_phase: str = (self.packet.read(1) != b'\x00')
        self.xid_format_id: int = struct.unpack('<I', self.packet.read(4))[0]
        gtrid_length = struct.unpack('<I', self.packet.read(4))[0]
        bqual_length = struct.unpack('<I', self.packet.read(4))[0]
        self.xid_gtrid: bytes = self.packet.read(gtrid_length)
        self.xid_bqual: bytes = self.packet.read(bqual_length)

    @property
    def xid(self) -> str:
        """Gets the xid python objects."""
        return self.xid_gtrid.decode() + self.xid_bqual.decode()

    def _dump(self) -> None:
        """Dumps the python objects for the event."""
        print("One phase: %s" % self.one_phase)
        print("XID formatID: %d" % self.xid_format_id)
        print("XID: %s" % self.xid)


class FormatDescriptionEvent(BinLogEvent):
    """
    The event represents a Format Description Event in the MySQL binary log.
    
    This event is written at the start of a binary log file for binlog version 4.
    It provides the necessary information to decode subsequent events in the file.

    In detail, the class creates the following python objects in the constructor:

    :ivar binlog_version: str - Version of the binary log format
    :ivar mysql_version_str: str - Server's MySQL version in string format
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection,
                 **kwargs: str) -> None:               
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.binlog_version: str = struct.unpack('<H', self.packet.read(2))
        self.mysql_version_str: str = self.packet.read(50).rstrip(b'\0').decode()
        numbers = self.mysql_version_str.split('-')[0]
        self.mysql_version: tuple[int, ...] = tuple(map(int, numbers.split('.')))

    def _dump(self) -> None:
        """Dumps the python objects for the event."""
        print("Binlog version: %s" % self.binlog_version)
        print("MySQL version: %s" % self.mysql_version_str)


class StopEvent(BinLogEvent):
    """Does nothing."""
    pass


class XidEvent(BinLogEvent):
    """
    A COMMIT event is generated when COMMIT of a transaction that modifies 
    one or more tables of an XA-capable storage engine occurs.

    For more information : `[see details] <https://mariadb.com/kb/en/xid_event/>`_.

    In detail, the class creates the following python objects in the constructor:
 
    :ivar xid: int - Transaction ID for 2 Phase Commit
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection,
                 **kwargs: str) -> None:
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.xid: int = struct.unpack('<Q', self.packet.read(8))[0]

    def _dump(self) -> None:
        """Dumps the python objects for the event."""
        super()._dump()
        print("Transaction ID: %d" % (self.xid))


class HeartbeatLogEvent(BinLogEvent):
    """
    A Heartbeat event
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

    In detail, the class creates the following python objects in the constructor:

    :ivar ident: str - Name of the current binlog
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection,
                 **kwargs: str) -> None:   
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.ident: str = self.packet.read(event_size).decode()

    def _dump(self) -> None:
        """Dumps the python objects for the event."""
        super()._dump()
        print("Current binlog: %s" % (self.ident))


class QueryEvent(BinLogEvent):
    """
    QueryEvent is generated for each query that modified database.
    If row-based replication is used, DML will not be logged as RowsEvent instead.

    In detail, the class creates the following python objects in the constructor:

    :ivar slave_proxy_id: int - The id of the thread that issued this statement on the master server
    :ivar execution_time: int - The time from when the query started to when it was logged in the binlog, in seconds
    :ivar schema_length: int - The length of the name of the currently selected database
    :ivar error_code: int - Error code generated by the master
    :ivar status_vars_length: int - The length of the status variable

    :ivar schema: str - The name of the currently selected database
    :ivar query: str - The query executed
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection,
                 **kwargs: str) -> None:            
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # Post-header
        self.slave_proxy_id: int = self.packet.read_uint32()
        self.execution_time: int = self.packet.read_uint32()
        self.schema_length: int = struct.unpack("!B", self.packet.read(1))[0]
        self.error_code: int = self.packet.read_uint16()
        self.status_vars_length: int = self.packet.read_uint16()

        # Payload
        status_vars_end_pos = self.packet.read_bytes + self.status_vars_length
        while self.packet.read_bytes < status_vars_end_pos:
            # read KEY for status variable
            status_vars_key = self.packet.read_uint8()
            # read VALUE for status variable
            self._read_status_vars_value_for_key(status_vars_key)

        self.schema: str = self.packet.read(self.schema_length)
        self.packet.advance(1)

        self.query: str = self.packet.read(event_size - 13 - self.status_vars_length
                                           - self.schema_length - 1).decode("utf-8")
        #string[EOF]    query

    def _dump(self) -> None:
        """Dump the python objects for the event."""
        super()._dump()
        print("Schema: %s" % (self.schema))
        print("Execution time: %d" % (self.execution_time))
        print("Query: %s" % (self.query))

    def _read_status_vars_value_for_key(self, key: int) -> Union[None, NoReturn]:
        """
        CAUTION: this function returns nothing(not None) or raises StatusVariableMismatch!

        It parses status variable VALUE for given KEY.

        A status variable in query events is a sequence of status KEY-VALUE pairs.
        Parsing logic from mysql-server source code edited by dongwook-chan
        https://github.com/mysql/mysql-server/blob/beb865a960b9a8a16cf999c323e46c5b0c67f21f/libbinlogevents/src/statement_events.cpp#L181-L336

        See details about status variable at:
        https://mariadb.com/kb/en/query_event/
        https://dev.mysql.com/doc/dev/mysql-server/latest/classbinary__log_1_1Query__event.html
        
        In detail, the function creates python objects depending on the given key:

        :ivar flags2: int
        :ivar sql_mode: int
        :ivar auto_increment_increment: int
        :ivar auto_increment_offset: int
        :ivar character_set_client: int
        :ivar collation_connection: int
        :ivar collation_server: int
        :ivar time_zone: str
        :ivar catalog_nz_code: str
        :ivar lc_time_names_number: int
        :ivar charset_database_number: int
        :ivar table_map_for_update: int
        :ivar user: str
        :ivar host: str
        :ivar mts_accessed_db_names: [str, ...]
        :ivar microseconds: int
        :ivar explicit_defaults_ts: bool
        :ivar ddl_xid: int
        :ivar default_collation_for_utf8mb4_number: int
        :ivar sql_require_primary_key: int
        :ivar default_table_encryption: int
        :ivar hrnow: int
        :ivar xid: int
        """
        if key == Q_FLAGS2_CODE:                      # 0x00
            self.flags2: int = self.packet.read_uint32()
        elif key == Q_SQL_MODE_CODE:                   # 0x01
            self.sql_mode: int = self.packet.read_uint64()
        elif key == Q_CATALOG_CODE:                   # 0x02 for MySQL 5.0.x
            pass
        elif key == Q_AUTO_INCREMENT:                 # 0x03
            self.auto_increment_increment: int = self.packet.read_uint16()
            self.auto_increment_offset: int = self.packet.read_uint16()
        elif key == Q_CHARSET_CODE:                   # 0x04
            self.character_set_client: int = self.packet.read_uint16()
            self.collation_connection: int = self.packet.read_uint16()
            self.collation_server: int = self.packet.read_uint16()
        elif key == Q_TIME_ZONE_CODE:                 # 0x05
            time_zone_len = self.packet.read_uint8()
            if time_zone_len:
                self.time_zone: str = self.packet.read(time_zone_len) 
        elif key == Q_CATALOG_NZ_CODE:                # 0x06
            catalog_len = self.packet.read_uint8()
            if catalog_len:
                self.catalog_nz_code: str = self.packet.read(catalog_len)
        elif key == Q_LC_TIME_NAMES_CODE:             # 0x07
            self.lc_time_names_number: int = self.packet.read_uint16()
        elif key == Q_CHARSET_DATABASE_CODE:          # 0x08
            self.charset_database_number: int = self.packet.read_uint16()
        elif key == Q_TABLE_MAP_FOR_UPDATE_CODE:      # 0x09
            self.table_map_for_update: int = self.packet.read_uint64()
        elif key == Q_MASTER_DATA_WRITTEN_CODE:       # 0x0A
            pass
        elif key == Q_INVOKER:                        # 0x0B
            user_len = self.packet.read_uint8()
            if user_len:
                self.user: str = self.packet.read(user_len)
            host_len = self.packet.read_uint8()
            if host_len:
                self.host: str = self.packet.read(host_len)
        elif key == Q_UPDATED_DB_NAMES:               # 0x0C
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
            self.mts_accessed_db_names: [str, ...] = dbs
        elif key == Q_MICROSECONDS:                   # 0x0D
            self.microseconds: int = self.packet.read_uint24()
        elif key == Q_COMMIT_TS:                      # 0x0E
            pass
        elif key == Q_COMMIT_TS2:                     # 0x0F
            pass
        elif key == Q_EXPLICIT_DEFAULTS_FOR_TIMESTAMP:# 0x10
            self.explicit_defaults_ts: bool = self.packet.read_uint8()
        elif key == Q_DDL_LOGGED_WITH_XID:            # 0x11
            self.ddl_xid: int = self.packet.read_uint64()
        elif key == Q_DEFAULT_COLLATION_FOR_UTF8MB4:  # 0x12
            self.default_collation_for_utf8mb4_number: int = self.packet.read_uint16()
        elif key == Q_SQL_REQUIRE_PRIMARY_KEY:        # 0x13
            self.sql_require_primary_key: int = self.packet.read_uint8()
        elif key == Q_DEFAULT_TABLE_ENCRYPTION:       # 0x14
            self.default_table_encryption: int = self.packet.read_uint8()
        elif key == Q_HRNOW:
            self.hrnow: int = self.packet.read_uint24()
        elif key == Q_XID:
            self.xid: int = self.packet.read_uint64()
        else:
            raise StatusVariableMismatch


class BeginLoadQueryEvent(BinLogEvent):
    """
    This event is written into the binary log file for LOAD DATA INFILE events
    if the server variable binlog_mode was set to "STATEMENT".

    In detail, the class creates the following python objects in the constructor:

    :ivar file_id: int - the id of the file
    :ivar block-data: str - data block about "LOAD DATA INFILE"
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection,
                 **kwargs: str) -> None:                
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # Payload
        self.file_id: int = self.packet.read_uint32()
        self.block_data: str = self.packet.read(event_size - 4)

    def _dump(self) -> None:
        """Dumps the python objects for the event."""
        super()._dump()
        print("File id: %d" % (self.file_id))
        print("Block data: %s" % (self.block_data))


class ExecuteLoadQueryEvent(BinLogEvent):
    """
    This event handles "LOAD DATA INFILE" statement.
    LOAD DATA INFILE statement reads data from file and insert into database's table.
    Since QueryEvent cannot explain this special action, ExecuteLoadQueryEvent is needed.
    So it is similar to a QUERY_EVENT except that it has extra static fields.

    In detail, the class creates the following python objects in the constructor:

    :ivar slave_proxy_id: int - The id of the thread that issued this statement on the master server
    :ivar execution_time: int - The number of seconds that the statement took to execute
    :ivar schema_length: int - The length of the default database's name when the statement was executed
    :ivar error_code: int - The error code resulting from execution of the statement on the master
    :ivar status_vars_length: int - The length of the status variable block
    :ivar file_id: int - The id of the loaded file
    :ivar start_pos: int - Offset from the start of the statement to the beginning of the filename
    :ivar end_pos: int - Offset from the start of the statement to the end of the filename
    :ivar dup_handling_flags: int - How LOAD DATA INFILE handles duplicated data (0x0: error, 0x1: ignore, 0x2: replace)
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection,
                 **kwargs: str) -> None:
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # Post-header
        self.slave_proxy_id: int = self.packet.read_uint32()
        self.execution_time: int = self.packet.read_uint32()
        self.schema_length: int = self.packet.read_uint8()
        self.error_code: int = self.packet.read_uint16()
        self.status_vars_length: int = self.packet.read_uint16()

        # Payload
        self.file_id: int = self.packet.read_uint32()
        self.start_pos: int = self.packet.read_uint32()
        self.end_pos: int = self.packet.read_uint32()
        self.dup_handling_flags: int = self.packet.read_uint8()

    def _dump(self) -> None:
        """Dumps the python objects for the event."""
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


class IntvarEvent(BinLogEvent):
    """
    The event stores the value of auto-increment variables.
    It will be created just before a QueryEvent.

    In detail, the class creates the following python objects in the constructor:

    :ivar type: int - 1 byte identifying the type of variable stored. \
    Can be either LAST_INSERT_ID_EVENT (1) or INSERT_ID_EVENT (2).
    :ivar value: int - The value of the variable
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection,
                 **kwargs: str) -> None:
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # Payload
        self.type: int = self.packet.read_uint8()
        self.value: int = self.packet.read_uint32()

    def _dump(self) -> None:
        """Dumps the python objects for the event."""
        super()._dump()
        print("type: %d" % (self.type))
        print("Value: %d" % (self.value))


class RandEvent(BinLogEvent):
    """
    RandEvent is generated every time a statement uses the RAND() function.
    It indicates the seed values to use for generating a random number with RAND() in the next statement.

    RandEvent only works in statement-based logging (need to set binlog_format as 'STATEMENT')
    and only works when the seed number is not specified.
    
    In detail, the class creates the following python objects in the constructor:

    :ivar seed1: int - value for the first seed
    :ivar seed2: int - value for the second seed    
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection, 
                 **kwargs: str):
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        # Payload
        self._seed1: int = self.packet.read_uint64()
        self._seed2: int = self.packet.read_uint64()

    @property
    def seed1(self) -> int:
        """Gets the first seed value."""
        return self._seed1

    @property
    def seed2(self) -> int:
        """Gets the second seed value."""
        return self._seed2

    def _dump(self) -> None:
        """Dumps the python objects for the event."""
        super()._dump()
        print("seed1: %d" % (self.seed1))
        print("seed2: %d" % (self.seed2))


class MariadbStartEncryptionEvent(BinLogEvent):
    """
    Since MariaDB 10.1.7, 
    the START_ENCRYPTION event is written to every binary log file 
    if encrypt_binlog is set to ON. Prior to enabling this setting, 
    additional configuration steps are required in MariaDB. 
    (Link: https://mariadb.com/kb/en/encrypting-binary-logs/)

    This event is written just once, after the Format Description event.

    In detail, the class creates the following python objects in the constructor:

    :ivar schema: int - The Encryption scheme, always set to 1 for system files
    :ivar key_version: int - The Encryption key version
    :ivar nonce: bytes - Nonce (12 random bytes) of current binlog file   
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection, 
                 **kwargs: str) -> None:    
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)

        self.schema: int = self.packet.read_uint8()
        self.key_version: int = self.packet.read_uint32()
        self.nonce: bytes = self.packet.read(12)

    def _dump(self) -> None:
        """Dumps the python objects for the event."""
        print("Schema: %d" % self.schema)
        print("Key version: %d" % self.key_version)
        print(f"Nonce: {self.nonce}")


class RowsQueryLogEvent(BinLogEvent):
    """
    This class records original query for the row events in Row-Based Replication.

    In detail, it creates the following python objects in the constructor:

    :ivar query_length: int - Length of the SQL statement
    :ivar query: str - The executed SQL statement

    More details are available in the MySQL Knowledge Base:
    https://dev.mysql.com/doc/dev/mysql-server/latest/classRows__query__log__event.html
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection, 
                 **kwargs: str) -> None:               
        super(RowsQueryLogEvent, self).__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.query_length: int = self.packet.read_uint8()
        self.query: str = self.packet.read(self.query_length).decode('utf-8')

    def dump(self) -> None:
        """Dumps the python objects for the event."""
        print("=== %s ===" % (self.__class__.__name__))
        print("Query length: %d" % self.query_length)
        print("Query: %s" % self.query)


class NotImplementedEvent(BinLogEvent):
    """
    This class is used as a temporary class for events that have not yet been implemented.

    The event class referencing this class skips parsing.
    """
    def __init__(self, 
                 from_packet: Type[MysqlPacket], 
                 event_size: int, 
                 table_map: Dict[int, Table], 
                 ctl_connection: pymysql.connections.Connection, 
                 **kwargs: str) -> None:    
        super().__init__(from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.packet.advance(event_size)
