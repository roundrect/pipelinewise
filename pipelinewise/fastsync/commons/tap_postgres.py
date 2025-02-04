import psycopg2
from psycopg2 import extras
import gzip
import datetime
import decimal

from . import utils


class FastSyncTapPostgres:
    def __init__(self, connection_config, tap_type_to_target_type):
        self.connection_config = connection_config
        self.tap_type_to_target_type = tap_type_to_target_type


    def open_connection(self):
        conn_string = "host='{}' port='{}' user='{}' password='{}' dbname='{}'".format(
            # Fastsync is using replica_{host|port|user|password} values from the config by default
            # to avoid making heavy load on the primary source database when syncing large tables
            #
            # If replica_{host|port|user|password} values are not defined in the config then it's
            # using the normal credentials to connect
            self.connection_config.get('replica_host', self.connection_config['host']),
            self.connection_config.get('replica_port', self.connection_config['port']),
            self.connection_config.get('replica_user', self.connection_config['user']),
            self.connection_config.get('replica_password', self.connection_config['password']),
            self.connection_config['dbname']
        )
        self.conn = psycopg2.connect(conn_string)
        # Set connection to autocommit
        self.conn.autocommit = True
        self.curr = self.conn.cursor()


    def close_connection(self):
        self.conn.close()


    def query(self, query, params=None):
        utils.log("POSTGRES - Running query: {}".format(query))
        with self.conn as connection:
            with connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    query,
                    params
                )

                if cur.rowcount > 0:
                    return cur.fetchall()
                else:
                    return []


    def primary_host_query(self, query, params=None):
        utils.log("POSTGRES - Running query: {}".format(query))
        with self.primary_host_conn as connection:
            with connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    query,
                    params
                )

                if cur.rowcount > 0:
                    return cur.fetchall()
                else:
                    return []


    def fetch_current_log_pos(self):
        # Create replication slot dedicated connection
        # Always use Primary server for creating replication_slot
        primary_host_conn_string = "host='{}' port='{}' user='{}' password='{}' dbname='{}'".format(

            self.connection_config['host'],
            self.connection_config['port'],
            self.connection_config['user'],
            self.connection_config['password'],
            self.connection_config['dbname']
        )
        self.primary_host_conn = psycopg2.connect(primary_host_conn_string)
        # Set connection to autocommit
        self.primary_host_conn.autocommit = True
        self.primary_host_curr = self.primary_host_conn.cursor()

        # Make sure PostgreSQL version is 9.4 or higher
        result = self.primary_host_query("SELECT setting::int AS version FROM pg_settings WHERE name='server_version_num'")
        version = result[0].get("version")

        # Do not allow minor versions with PostgreSQL BUG #15114
        if (version >= 110000) and (version < 110002):
            raise Exception('PostgreSQL upgrade required to minor version 11.2')
        elif (version >= 100000) and (version < 100007):
            raise Exception('PostgreSQL upgrade required to minor version 10.7')
        elif (version >= 90600) and (version < 90612):
            raise Exception('PostgreSQL upgrade required to minor version 9.6.12')
        elif (version >= 90500) and (version < 90516):
            raise Exception('PostgreSQL upgrade required to minor version 9.5.16')
        elif (version >= 90400) and (version < 90421):
            raise Exception('PostgreSQL upgrade required to minor version 9.4.21')
        elif (version < 90400):
            raise Exception('Logical replication not supported before PostgreSQL 9.4')

        # Create replication slot, ignore error if already exists
        try:
            result = self.primary_host_query("SELECT * FROM pg_create_logical_replication_slot('pipelinewise_{}', 'wal2json')".format(self.connection_config['dbname'].lower()))
        except Exception as e:
            # ERROR: replication slot "stitch_{}" already exists SQL state: 42710
            if (e.pgcode == '42710'):
                pass
            else:
                raise e

        # Close replication slot dedicated connection
        self.primary_host_conn.close()

        # is replica_host set ?
        if self.connection_config.get('replica_host'):
            # Get latest applied lsn from replica_host
            if version >= 100000:
                result = self.query("SELECT pg_last_wal_replay_lsn() AS current_lsn")
            elif version >= 90400:
                result = self.query("SELECT pg_last_xlog_replay_location() AS current_lsn")
            else:
                raise Exception('Logical replication not supported before PostgreSQL 9.4')
        else:
            # Get current lsn from primary host
            if version >= 100000:
                result = self.query("SELECT pg_current_wal_lsn() AS current_lsn")
            elif version >= 90400:
                result = self.query("SELECT pg_current_xlog_location() AS current_lsn")
            else:
                raise Exception('Logical replication not supported before PostgreSQL 9.4')

        current_lsn = result[0].get("current_lsn")
        file, index = current_lsn.split('/')
        lsn = (int(file, 16)  << 32) + int(index, 16)

        return {
          "lsn": lsn,
          "version": 1
        }


    def fetch_current_incremental_key_pos(self, table, replication_key):
        result = self.query("SELECT MAX({}) AS key_value FROM {}".format(replication_key, table))
        if len(result) == 0:
            raise Exception("Cannot get replication key value for table: {}".format(table))
        else:
            postgres_key_value = result[0].get("key_value")
            key_value = postgres_key_value

            # Convert postgres data/datetime format to JSON friendly values
            if isinstance(postgres_key_value, datetime.datetime):
                key_value = postgres_key_value.isoformat()

            elif isinstance(postgres_key_value, datetime.date):
                key_value = postgres_key_value.isoformat() + 'T00:00:00'

            elif isinstance(postgres_key_value, decimal.Decimal):
                key_value = float(postgres_key_value)

            return {
                "replication_key": replication_key,
                "replication_key_value": key_value,
                "version": 1
            }


    def get_primary_key(self, table):
        sql = """SELECT pg_attribute.attname
                    FROM pg_index, pg_class, pg_attribute, pg_namespace
                    WHERE
                        pg_class.oid = '{}'::regclass AND
                        indrelid = pg_class.oid AND
                        pg_class.relnamespace = pg_namespace.oid AND
                        pg_attribute.attrelid = pg_class.oid AND
                        pg_attribute.attnum = any(pg_index.indkey)
                    AND indisprimary""".format(table)
        pk = self.query(sql)
        if len(pk) > 0:
            return pk[0][0]
        else:
            return None


    def get_table_columns(self, table_name):
        table_dict = utils.tablename_to_dict(table_name)
        sql = """
                SELECT
                    column_name
                    ,data_type
                    ,safe_sql_value
                FROM (SELECT
                column_name,
                data_type,
                CASE
                    WHEN data_type = 'ARRAY' THEN 'array_to_json(' || column_name || ') AS ' || column_name
                    WHEN udt_name = 'time' THEN 'replace(' || column_name || E'::varchar,\\\'24:00:00\\\',\\\'00:00:00\\\') AS ' || column_name
                    WHEN udt_name = 'timetz' THEN 'replace(' || column_name || E'::time::varchar,\\\'24:00:00\\\',\\\'00:00:00\\\') AS ' || column_name
                    ELSE column_name
                END AS safe_sql_value
                FROM information_schema.columns
                WHERE table_schema = '{}'
                    AND table_name = '{}'
                ORDER BY ordinal_position
                ) AS x
            """.format(table_dict.get('schema_name'), table_dict.get('table_name'))
        return self.query(sql)


    def map_column_types_to_target(self, table_name):
        postgres_columns = self.get_table_columns(table_name)
        mapped_columns = ["{} {}".format(pc[0], self.tap_type_to_target_type(pc[1])) for pc in postgres_columns]

        return {
            "columns": mapped_columns,
            "primary_key": self.get_primary_key(table_name)
        }


    def copy_table(self, table_name, path):
        table_columns = self.get_table_columns(table_name)
        column_safe_sql_values = [c.get('safe_sql_value') for c in table_columns]

        # If self.get_table_columns returns zero row then table not exist
        if len(column_safe_sql_values) == 0:
            raise Exception("{} table not found.".format(table_name))

        sql = """COPY (SELECT {}
        ,now() AT TIME ZONE 'UTC'
        ,now() AT TIME ZONE 'UTC'
        ,null
        FROM {}) TO STDOUT with CSV DELIMITER ','
        """.format(','.join(column_safe_sql_values), table_name)
        utils.log("POSTGRES - Exporting data: {}".format(sql))
        with gzip.open(path, 'wt') as gzfile:
            self.curr.copy_expert(sql, gzfile, size=131072)