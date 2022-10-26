import json
import os
import base64
import time

import environs
import requests
from mysql.connector.errors import Error
from . import log
from . import defines

headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}


def format_result(results):
    res = ""
    if results is None:
        return ""

    for line in results:
        buf = ""
        for item in line:
            if isinstance(item, bool):
                item = str.lower(str(item))
            if buf == "":
                buf = str(item)
            else:
                buf = buf + " " + str(item)  # every item seperate by space
        if len(buf) == 0:
            # empty line in results will replace with tab
            buf = "\t"
        res = res + buf + "\n"
    return res


def get_data_type(field):
    if 'data_type' in field:
        if 'inner' in field['data_type']:
            return field['data_type']['inner']['type']
        else:
            return field['data_type']['type']


def get_query_options(response):
    ret = ""
    if get_error(response) is not None:
        return ret
    for field in response['schema']['fields']:
        typ = str.lower(get_data_type(field))
        log.debug(f"type:{typ}")
        if "int" in typ:
            ret = ret + "I"
        elif "float" in typ or "double" in typ:
            ret = ret + "F"
        elif "bool" in typ:
            ret = ret + "B"
        else:
            ret = ret + "T"
    return ret


def get_next_uri(response):
    if "next_uri" in response:
        return response['next_uri']
    return None


def get_result(response):
    return response['data']


def get_error(response):
    if response['error'] is None:
        return None

    # Wrap errno into msg, for result check
    return Error(msg=response['error']['message'],
                 errno=response['error']['code'])


class Connection(object):
    # Databend http handler doc: https://databend.rs/doc/reference/api/rest

    # Call connect(**driver)
    # driver is a dict contains:
    # {
    #   'user': 'root',
    #   'host': '127.0.0.1',
    #   'port': 3307,
    #   'database': 'default'
    # }
    def __init__(self, host, port=None, user=defines.DEFAULT_USER, password=defines.DEFAULT_PASSWORD,
                 database=defines.DEFAULT_DATABASE, secure=False, ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.secure = secure
        self.session_max_idle_time = defines.DEFAULT_SESSION_IDLE_TIME
        self.session = {}
        self.additional_headers = dict()
        self.query_option = None
        self.schema = 'http'
        if self.secure:
            self.schema = 'https'
        e = environs.Env()
        if os.getenv("ADDITIONAL_HEADERS") is not None:
            self.additional_headers = e.dict("ADDITIONAL_HEADERS")

    def make_headers(self):
        if "Authorization" not in self.additional_headers:
            return {
                **headers, "Authorization":
                    "Basic " + base64.b64encode("{}:{}".format(
                        self.user, self.password).encode(encoding="utf-8")).decode()
            }
        else:
            return {**headers, **self.additional_headers}

    def get_description(self):
        return '{}:{}'.format(self.host, self.port)

    def disconnect(self):
        self._session = {}

    def query(self, statement, session):
        url = self.format_url()
        log.logger.debug(f"http sql: {statement}")
        query_sql = {'sql': statement, "string_fields": True}
        if session is not None:
            query_sql['session'] = session
        log.logger.debug(f"http headers {self.make_headers()}")
        response = requests.post(url,
                                 data=json.dumps(query_sql),
                                 headers=self.make_headers(), verify=False)

        try:
            return json.loads(response.content)
        except Exception as err:
            log.logger.error(
                f"http error, SQL: {statement}\ncontent: {response.content}\nerror msg:{str(err)}"
            )
            raise

    def format_url(self):
        return f"{self.schema}://{self.host}:{self.port}/v1/query/"

    def reset_session(self):
        self._session = {}

    def next_page(self, next_uri):
        url = "{}://{}:{}{}".format(self.schema, self.host, self.port, next_uri)
        return requests.get(url=url, headers=self.make_headers())

    # return a list of response util empty next_uri
    def query_with_session(self, statement):
        current_session = self._session
        response_list = list()
        response = self.query(statement, current_session)
        log.logger.debug(f"response content: {response}")
        response_list.append(response)
        start_time = time.time()
        time_limit = 12
        session = response['session']
        if session:
            self._session = session
        while response['next_uri'] is not None:
            resp = self.next_page(response['next_uri'])
            response = json.loads(json.loads(resp.content))
            log.logger.debug(f"Sql in progress, fetch next_uri content: {response}")
            self.check_error(response)
            session = response['session']
            if session:
                self._session = session
            response_list.append(response)
            if time.time() - start_time > time_limit:
                log.logger.warning(
                    f"after waited for {time_limit} secs, query still not finished (next uri not none)!"
                )
        return response_list

    def check_error(self, resp):
        error = get_error(resp)
        if error:
            raise error

    def fetch_all(self, statement):
        resp_list = self.query_with_session(statement)
        if len(resp_list) == 0:
            log.logger.warning("fetch all with empty results")
            return None
        self._query_option = get_query_options(resp_list[0])  # record schema
        data_list = list()
        for response in resp_list:
            data = get_result(response)
            if len(data) != 0:
                data_list.extend(data)
        return data_list

    def get_query_option(self):
        return self._query_option

#
# if __name__ == '__main__':
#     from config import http_config
#     connector = HttpConnector()
#     connector.connect(**http_config)
#     connector.query_without_session("show databases;")