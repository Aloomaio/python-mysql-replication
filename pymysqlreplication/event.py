# -*- coding: utf-8 -*-

import binascii
import re
import struct
import datetime
import chardet

from pymysql.util import byte2int, int2byte
from pymysqlreplication import utils


class BinLogEvent(object):
    __slots__ = (
        'packet', 'table_map', 'event_type', 'timestamp', 'event_size',
        '_ctl_connection', '_fail_on_table_metadata_unavailable', '_processed',
        'complete'
    )

    def __init__(self, from_packet, event_size, table_map, ctl_connection,
                 only_tables=None,
                 ignored_tables=None,
                 only_schemas=None,
                 ignored_schemas=None,
                 freeze_schema=False,
                 fail_on_table_metadata_unavailable=False):
        self.packet = from_packet
        self.table_map = table_map
        self.event_type = self.packet.event_type
        self.timestamp = self.packet.timestamp
        self.event_size = event_size
        self._ctl_connection = ctl_connection
        self._fail_on_table_metadata_unavailable = fail_on_table_metadata_unavailable
        # The event have been fully processed, if processed is false
        # the event will be skipped
        self._processed = True
        self.complete = True

    def _read_table_id(self):
        # Table ID is 6 byte
        # pad little-endian number
        table_id = self.packet.read(6) + int2byte(0) + int2byte(0)
        return struct.unpack('<Q', table_id)[0]

    def dump(self):
        print("=== %s ===" % (self.__class__.__name__))
        print("Date: %s" % (datetime.datetime.fromtimestamp(self.timestamp)
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
    """
    __slots__ = ('commit_flag', 'sid', 'gno')

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(GtidEvent, self).__init__(from_packet, event_size, table_map,
                                          ctl_connection, **kwargs)

        self.commit_flag = byte2int(self.packet.read(1)) == 1
        self.sid = self.packet.read(16)
        self.gno = struct.unpack('<Q', self.packet.read(8))[0]

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

    def __repr__(self):
        return '<GtidEvent "%s">' % self.gtid


class RotateEvent(BinLogEvent):
    """Change MySQL bin log file

    Attributes:
        position: Position inside next binlog
        next_binlog: Name of next binlog file
    """
    __slots__ = ('position', 'next_binlog')

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(RotateEvent, self).__init__(from_packet, event_size, table_map,
                                          ctl_connection, **kwargs)
        self.position = struct.unpack('<Q', self.packet.read(8))[0]
        rest_of_event_data = self.packet.read(event_size - 8)
        try:
            self.next_binlog = re.findall(b'.+\.\d{6}', rest_of_event_data)[0].decode()

        except IndexError:  # We failed to parse the next binlog file name
            self.next_binlog = None

    def dump(self):
        print("=== %s ===" % (self.__class__.__name__))
        print("Position: %d" % self.position)
        print("Next binlog file: %s" % self.next_binlog)
        print()


class FormatDescriptionEvent(BinLogEvent):
    __slots__ = ('binlog_version', 'server_version', 'has_checksum')

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(FormatDescriptionEvent, self).__init__(from_packet, event_size, table_map,
                                          ctl_connection, **kwargs)
        self.binlog_version = struct.unpack('<H', self.packet.read(2))[0]
        self.server_version = self.packet.read(50).rstrip(b'\x00').decode()
        self.has_checksum = False
        if utils.is_checksum_supported(self.server_version):
            event_size_without_header = self.packet.event_size - 19
            # skip event types and stop reading before 5 last chars that
            # representing checksum algorithm (1) + checksum (4)
            self.packet.read((event_size_without_header - 2 - 50) - 5)

            # if checksum algorithm type is CRC32 (=1)
            if struct.unpack('b', self.packet.read(1))[0] == 1:
                self.has_checksum = True
            # 4 remaining bytes - checksum itself

    def dump(self):
        print("=== %s ===" % (self.__class__.__name__))
        print("Binlog Version: %s" % self.binlog_version)
        print("Server Version: %s" % self.server_version)
        print()


class StopEvent(BinLogEvent):
    pass


class XidEvent(BinLogEvent):
    """A COMMIT event

    Attributes:
        xid: Transaction ID for 2PC
    """
    __slots__ = ('xid',)

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(XidEvent, self).__init__(from_packet, event_size, table_map,
                                       ctl_connection, **kwargs)
        self.xid = struct.unpack('<Q', self.packet.read(8))[0]

    def _dump(self):
        super(XidEvent, self)._dump()
        print("Transaction ID: %d" % self.xid)


class HeartbeatLogEvent(BinLogEvent):
    """A Heartbeat event
    Heartbeats are sent by the master only if there are no unsent events in the
    binary log file for a period longer than the interval defined by
    MASTER_HEARTBEAT_PERIOD connection setting.

    A mysql server will also play those to the slave for each skipped
    events in the log. I (baloo) believe the intention is to make the slave
    bump its position so that if a disconnection occurs, the slave only
    reconnects from the last skipped position (see Binlog_sender::send_events
    in sql/rpl_binlog_sender.cc). That makes 106 bytes of data for skipped
    event in the binlog. *this is also the case with GTID replication*. To
    mitigate such behavior, you are expected to keep the binlog small (see
    max_binlog_size, defaults to 1G).
    In any case, the timestamp is 0 (as in 1970-01-01T00:00:00).

    Attributes:
        ident: Name of the current binlog
    """
    __slots__ = ('ident',)

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(HeartbeatLogEvent, self).__init__(from_packet, event_size,
                                                table_map, ctl_connection,
                                                **kwargs)
        self.ident = self.packet.read(event_size).decode()

    def _dump(self):
        super(HeartbeatLogEvent, self)._dump()
        print("Current binlog: %s" % self.ident)


class QueryEvent(BinLogEvent):
    """
    This event is trigger when a query is run of the database.
    Only replicated queries are logged.
    """
    __slots__ = (
        'slave_proxy_id', 'execution_time', 'schema_length', 'error_code',
        'status_vars_length', 'status_vars', 'schema', 'query'
    )

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(QueryEvent, self).__init__(from_packet, event_size, table_map,
                                         ctl_connection, **kwargs)

        # Post-header
        self.slave_proxy_id = self.packet.read_uint32()
        self.execution_time = self.packet.read_uint32()
        self.schema_length = byte2int(self.packet.read(1))
        self.error_code = self.packet.read_uint16()
        self.status_vars_length = self.packet.read_uint16()

        # Payload
        self.status_vars = self.packet.read(self.status_vars_length)
        self.schema = self.packet.read(self.schema_length)
        self.packet.advance(1)

        self.query = self.packet.read(event_size - 13 - self.status_vars_length
                                      - self.schema_length - 1)
        self.query = self._decode_query(self.query)
        #string[EOF]    query

    def _dump(self):
        super(QueryEvent, self)._dump()
        print("Schema: %s" % (self.schema))
        print("Execution time: %d" % (self.execution_time))
        print("Query: %s" % (self.query))

    @staticmethod
    def _decode_query(query):
        if not query:
            return query

        try:
            encoded_query = query.decode("utf-8")

        except UnicodeError:
            try:
                encoding = chardet.detect(query)['encoding']
                encoded_query = query.decode(encoding)

            except (TypeError, UnicodeError):  # Unrecognized encoding
                encoded_query = query

        return encoded_query


class BeginLoadQueryEvent(BinLogEvent):
    """

    Attributes:
        file_id
        block-data
    """
    __slots__ = ('file_id', 'block_data')

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(BeginLoadQueryEvent, self).__init__(from_packet, event_size, table_map,
                                                     ctl_connection, **kwargs)

        # Payload
        self.file_id = self.packet.read_uint32()
        self.block_data = self.packet.read(event_size - 4)

    def _dump(self):
        super(BeginLoadQueryEvent, self)._dump()
        print("File id: %d" % self.file_id)
        print("Block data: %s" % self.block_data)


class ExecuteLoadQueryEvent(BinLogEvent):
    """

    Attributes:
        slave_proxy_id
        execution_time
        schema_length
        error_code
        status_vars_length

        file_id
        start_pos
        end_pos
        dup_handling_flags
    """
    __slots__ = ('slave_proxy_id', 'execution_time', 'schema_length',
                 'error_code', 'status_vars_length', 'file_id', 'start_pos',
                 'end_pos', 'dup_handling_flags')

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(ExecuteLoadQueryEvent, self).__init__(from_packet, event_size, table_map,
                                                        ctl_connection, **kwargs)

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
        print("Slave proxy id: %d" % self.slave_proxy_id)
        print("Execution time: %d" % self.execution_time)
        print("Schema length: %d" % self.schema_length)
        print("Error code: %d" % self.error_code)
        print("Status vars length: %d" % self.status_vars_length)
        print("File id: %d" % self.file_id)
        print("Start pos: %d" % self.start_pos)
        print("End pos: %d" % self.end_pos)
        print("Dup handling flags: %d" % self.dup_handling_flags)


class IntvarEvent(BinLogEvent):
    """

    Attributes:
        type
        value
    """
    __slots__ = ('type', 'value')

    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(IntvarEvent, self).__init__(from_packet, event_size, table_map,
                                          ctl_connection, **kwargs)

        # Payload
        self.type = self.packet.read_uint8()
        self.value = self.packet.read_uint32()

    def _dump(self):
        super(IntvarEvent, self)._dump()
        print("type: %d" % self.type)
        print("Value: %d" % self.value)


class NotImplementedEvent(BinLogEvent):
    def __init__(self, from_packet, event_size, table_map, ctl_connection, **kwargs):
        super(NotImplementedEvent, self).__init__(
            from_packet, event_size, table_map, ctl_connection, **kwargs)
        self.packet.advance(event_size)
