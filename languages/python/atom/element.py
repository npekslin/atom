import redis
import threading
import time
import uuid
import os
from atom.config import DEFAULT_REDIS_PORT, DEFAULT_REDIS_SOCKET, HEALTHCHECK_RETRY_INTERVAL
from atom.config import LANG, VERSION, ACK_TIMEOUT, RESPONSE_TIMEOUT, STREAM_LEN, MAX_BLOCK
from atom.config import ATOM_NO_ERROR, ATOM_COMMAND_NO_ACK, ATOM_COMMAND_NO_RESPONSE
from atom.config import ATOM_COMMAND_UNSUPPORTED, ATOM_CALLBACK_FAILED, ATOM_USER_ERRORS_BEGIN
from atom.config import HEALTHCHECK_COMMAND, VERSION_COMMAND, REDIS_PIPELINE_POOL_SIZE, COMMAND_LIST_COMMAND
from atom.messages import Cmd, Response, StreamHandler, format_redis_py
from atom.messages import Acknowledge, Entry, Log, LogLevel, ENTRY_RESERVED_KEYS
import atom.serialization as ser
from os import uname
from queue import Queue


class Element:
    def __init__(self, name, host=None, port=DEFAULT_REDIS_PORT, socket_path=DEFAULT_REDIS_SOCKET):
        """
        Args:
            name (str): The name of the element to register with Atom.
            host (str, optional): The ip address of the Redis server to connect to.
            port (int, optional): The port of the Redis server to connect to.
            socket_path (str, optional): Path to Redis Unix socket.
        """
        self.name = name
        self.host = uname().nodename
        self.handler_map = {}
        self.timeouts = {}
        self.streams = set()
        self._rclient = None
        self._command_loop_shutdown = threading.Event()
        self._rpipeline_pool = Queue()
        self.reserved_commands = [COMMAND_LIST_COMMAND, VERSION_COMMAND, HEALTHCHECK_COMMAND]
        try:
            if host is not None:
                self._rclient = redis.StrictRedis(host=host, port=port)
            else:
                self._rclient = redis.StrictRedis(unix_socket_path=socket_path)

            # Init our pool of redis clients/pipelines
            for i in range(REDIS_PIPELINE_POOL_SIZE):
                self._rpipeline_pool.put(self._rclient.pipeline())

            _pipe = self._rpipeline_pool.get()
            _pipe.xadd(
                self._make_response_id(self.name),
                {
                    "language": LANG,
                    "version": VERSION
                },
                maxlen=STREAM_LEN)
            # Keep track of response_last_id to know last time the client's response stream was read from
            self.response_last_id = _pipe.execute()[-1].decode()
            self.response_last_id_lock = threading.Lock()

            _pipe.xadd(
                self._make_command_id(self.name),
                {
                    "language": LANG,
                    "version": VERSION
                },
                maxlen=STREAM_LEN)
            # Keep track of command_last_id to know last time the element's command stream was read from
            self.command_last_id = _pipe.execute()[-1].decode()
            _pipe = self._release_pipeline(_pipe)

            # Init a default healthcheck, overridable
            # By default, if no healthcheck is set, we assume everything is ok and return error code 0
            self.healthcheck_set(lambda: Response())

            # Init a version check callback which reports our language/version
            current_major_version = ".".join(VERSION.split(".")[:-1])
            self.command_add(
                VERSION_COMMAND,
                lambda: Response(data={"language": LANG, "version": float(current_major_version)}, serialization="msgpack")
            )

            # Add command to query all commands
            self.command_add(
                COMMAND_LIST_COMMAND,
                lambda: Response(data=[k for k in self.handler_map if k not in self.reserved_commands], serialization="msgpack")
            )

            # Load lua scripts
            self._stream_reference_sha = None
            this_dir, this_filename = os.path.split(__file__)
            with open(os.path.join(this_dir, 'stream_reference.lua')) as f:
                data = f.read()
                _pipe = self._rpipeline_pool.get()
                _pipe.script_load(data)
                script_response = _pipe.execute()
                _pipe = self._release_pipeline(_pipe)

                if (type(script_response) != list) or (len(script_response)) != 1 or (type(script_response[0]) != str):
                    self.log(LogLevel.ERROR, "Failed to load lua script stream_reference.lua")
                else:
                    self._stream_reference_sha = script_response[0]

            self.log(LogLevel.INFO, "Element initialized.", stdout=False)

        except redis.exceptions.RedisError:
            raise Exception("Could not connect to nucleus!")

    def __repr__(self):
        return f"{self.__class__.__name__}({self.name})"

    def clean_up_stream(self, stream):
        """
        Deletes the specified stream.

        Args:
            stream (string): The stream to delete.
        """
        if stream not in self.streams:
            raise Exception(f"Stream {stream} does not exist!")
        self._rclient.delete(self._make_stream_id(self.name, stream))
        self.streams.remove(stream)

    def __del__(self):
        """
        Removes all elements with the same name.
        """
        for stream in self.streams.copy():
            self.clean_up_stream(stream)
        try:
            self._rclient.delete(self._make_response_id(self.name))
            self._rclient.delete(self._make_command_id(self.name))
        except redis.exceptions.RedisError:
            raise Exception("Could not connect to nucleus!")

    def _release_pipeline(self, pipeline):
        """
        Resets the specified pipeline and returns it to the pool of available pipelines.

        Args:
            pipeline (Redis Pipeline): The pipeline to release
        """
        pipeline.reset()
        self._rpipeline_pool.put(pipeline)
        return None

    def _update_response_id_if_older(self, new_id):
        """
        Atomically update global response_last_id to new id, if timestamp on new id is more recent

        Args:
            new_id (str): New response id we want to set
        """
        self.response_last_id_lock.acquire()
        components = self.response_last_id.split("-")
        global_id_time = int(components[0])
        global_id_seq = int(components[1])
        components = new_id.split("-")
        new_id_time = int(components[0])
        new_id_seq = int(components[1])
        if (new_id_time > global_id_time or (new_id_time == global_id_time and new_id_seq > global_id_seq)):
            self.response_last_id = new_id
        self.response_last_id_lock.release()

    def _make_response_id(self, element_name):
        """
        Creates the string representation for a element's response stream id.

        Args:
            element_name (str): Name of the element to generate the id for.
        """
        return f"response:{element_name}"

    def _make_command_id(self, element_name):
        """
        Creates the string representation for an element's command stream id.

        Args:
            element_name (str): Name of the element to generate the id for.
        """
        return f"command:{element_name}"

    def _make_stream_id(self, element_name, stream_name):
        """
        Creates the string representation of an element's stream id.

        Args:
            element_name (str): Name of the element to generate the id for.
            stream_name (str): Name of element_name's stream to generate the id for.
        """
        if element_name is None:
            return stream_name
        else:
            return f"stream:{element_name}:{stream_name}"

    def _make_reference_id(self):
        """
        Creates a reference ID

        Args:

        """

        return f"reference:{self.name}:{str(uuid.uuid4())}"

    def _get_redis_timestamp(self):
        """
        Gets the current timestamp from Redis.
        """
        secs, msecs = self._rclient.time()
        timestamp = str(secs) + str(msecs).zfill(6)[:3]
        return timestamp

    def _decode_entry(self, entry):
        """
        Decodes the binary keys of an entry

        Args:
            entry (dict): The entry in dictionary form to decode.
        Returns:
            The decoded entry as a dictionary.
        """
        decoded_entry = {}
        for k in list(entry.keys()):
            if type(k) is bytes:
                k_str = k.decode()
            else:
                k_str = k
            decoded_entry[k_str] = entry[k]
        return decoded_entry

    def _deserialize_entry(self, entry, method=None):
        """
        Deserializes the binary data of the entry.

        Args:
            entry (dict): The entry in dictionary form to deserialize.
            method (str, optional): The method of deserialization to use;
                                    defaults to None.
        Returns:
            The deserialized entry as a dictionary.
        """
        for k, v in entry.items():
            if type(v) is bytes:
                try:
                    entry[k] = ser.deserialize(v, method=method)
                except:
                    pass
        return entry

    def _check_element_version(self, element_name, supported_language_set=None, supported_min_version=None):
        """
        Convenient helper function to query an element about whether it meets min language and version requirements for some feature

        Args:
            element_name (str): Name of the element to query
            supported_language_set (set, optional): Optional set of supported languages target element must be a part of to pass
            supported_min_version (float, optional): Optional min version target element must meet to pass
        """
        # Check if element is reachable and supports the version command
        response = self.get_element_version(element_name)
        if response["err_code"] != ATOM_NO_ERROR or type(response["data"]) is not dict:
            return False
        # Check for valid response to version command
        if not ("version" in response["data"] and "language" in response["data"] and type(response["data"]["version"]) is float):
            return False
        # Validate element meets language requirement
        if supported_language_set and response["data"]["language"] not in supported_language_set:
            return False
        # Validate element meets version requirement
        if supported_min_version and response["data"]["version"] < supported_min_version:
            return False
        return True

    def _get_serialization_method(self, data, user_serialization, force_serialization, deserialize=None):
        """
        Helper function to make a unified serialization decision based off of
        common user arguments. The serialization method returned will
        be a string, that will be based on the following logic:

        1. If `force_serialization` is true, then return the user-passed
            serialization method
        2. If the `ser` key is present in the data, go with that
        3. If the `ser` key is not present and the `deserialize` param is
            present then the type is `msgpack`
        4. Else, leave the data alone

        Args:
            data (dict): set of keys through which to search for special
                serialization key "ser".
            user_serialization (none/str): User-passed argument to API
            force_serializstion (bool): Boolean to ignore "ser" key if found
                in favor of the user-passed serialization. This can be useful
                if data is being read from atom in order to then move it
                through another transport layer which still needs the
                serialization
            deserialize (none/bool): Legacy param. If not equal to none, implies
                user_serialization = "msgpack"
        """

        serialization = user_serialization

        if not force_serialization:
            if "ser" in data.keys():
                serialization = data.pop("ser")
                if type(serialization) != str:
                    serialization = serialization.decode()
            elif deserialize is not None:  # check for deprecated legacy mode
                serialization = "msgpack" if deserialize else None

        return serialization

    def get_all_elements(self):
        """
        Gets the names of all the elements connected to the Redis server.

        Returns:
            List of element ids connected to the Redis server.
        """
        elements = [
            element.decode().split(":")[-1]
            for element in self._rclient.keys(self._make_response_id("*"))
        ]
        return elements

    def get_all_streams(self, element_name="*"):
        """
        Gets the names of all the streams of the specified element (all by default).

        Args:
            element_name (str): Name of the element of which to get the streams from.

        Returns:
            List of Stream ids belonging to element_name
        """
        streams = [
            stream.decode()
            for stream in self._rclient.keys(self._make_stream_id(element_name, "*"))
        ]
        return streams

    def get_element_version(self, element_name):
        """
        Queries the version info for the given element name.

        Args:
            element_name (str): Name of the element to query

        Returns:
            A dictionary of the response from the command.
        """
        return self.command_send(element_name, VERSION_COMMAND, "", serialization="msgpack")

    def get_all_commands(self, element_name=None, ignore_caller=True):
        """
        Gets the names of the commands of the specified element (all elements by default).

        Args:
            element_name (str): Name of the element of which to get the commands.
            ignore_caller (bool): Do not send commands to the caller.

        Returns:
            List of available commands for all elements or specified element.
        """
        elements = self.get_all_elements() if element_name is None else [element_name]
        if ignore_caller and self.name in elements:
            elements.remove(self.name)

        command_list = []
        for element in elements:
            # Check support for command_list command
            if self._check_element_version(element, {'Python'}, 0.3):
                # Retrieve commands for each element
                elem_commands = self.command_send(element, COMMAND_LIST_COMMAND, serialization="msgpack")['data']
                # Rename each command pre-pending the element name
                command_list.extend([f'{element}:{command}' for command in elem_commands])
        return command_list

    def command_add(self, name, handler, timeout=RESPONSE_TIMEOUT, serialization=None, deserialize=None):
        """
        Adds a command to the element for another element to call.

        Args:
            name (str): Name of the command.
            handler (callable): Function to call given the command name.
            timeout (int, optional): Time for the caller to wait for the command to finish.
            serialization (str, optional): The method of serialization to use;
                                           defaults to None.

            Deprecated:
            deserialize (bool, optional): Whether or not to deserialize the data using
                                          msgpack before passing it to the handler.
        """
        if not callable(handler):
            raise TypeError("Passed in handler is not a function!")
        if name in self.reserved_commands and name in self.handler_map:
            raise ValueError(f"'{name}' is a reserved command name dedicated to {name} commands, choose another name")

        if deserialize is not None:  # check for deprecated legacy mode
            serialization = "msgpack" if deserialize else None

        if not ser.is_valid_serialization(serialization):
            raise ValueError(f"Invalid serialization method \"{serialization}\"."
                             "Must be one of {ser.Serializations.print_values()}.")

        self.handler_map[name] = {"handler": handler, "serialization": serialization}

        self.timeouts[name] = timeout

    def healthcheck_set(self, handler):
        """
        Sets a custom healthcheck callback

        Args:
            handler (callable): Function to call when evaluating whether this element is healthy or not.
                                Should return a Response with err_code ATOM_NO_ERROR if healthy.
        """
        if not callable(handler):
            raise TypeError("Passed in handler is not a function!")
        # Handler must return response with 0 error_code to pass healthcheck
        self.handler_map[HEALTHCHECK_COMMAND] = {"handler": handler, "serialization": None}
        self.timeouts[HEALTHCHECK_COMMAND] = RESPONSE_TIMEOUT

    def wait_for_elements_healthy(self, element_list, retry_interval=HEALTHCHECK_RETRY_INTERVAL, strict=False):
        """
        Blocking call will wait until all elements in the element respond that they are healthy.

        Args:
            element_list ([str]): List of element names to run healthchecks on
                                  Should return a Response with err_code ATOM_NO_ERROR if healthy.
            retry_interval (float, optional) Time in seconds to wait before retrying after a failed attempt.
            strict (bool, optional) In strict mode, all elements must be reachable and support healthchecks to pass.
                                    If false, elements that don't have healthchecks will be assumed healthy.
        """

        while True:
            all_healthy = True
            for element_name in element_list:
                # Verify element is reachable and supports healthcheck feature
                if not self._check_element_version(element_name, supported_language_set={LANG}, supported_min_version=0.2):
                    # In strict mode, if element is not reachable or doesn't support healthchecks, assume unhealthy
                    if strict:
                        self.log(LogLevel.WARNING, f"Failed healthcheck on {element_name}, retrying...")
                        all_healthy = False
                        break
                    else:
                        continue

                response = self.command_send(element_name, HEALTHCHECK_COMMAND, "")
                if response["err_code"] != ATOM_NO_ERROR:
                    self.log(LogLevel.WARNING, f"Failed healthcheck on {element_name}, retrying...")
                    all_healthy = False
                    break
            if all_healthy:
                break

            time.sleep(retry_interval)

    def command_loop(self):
        """
        Waits for command to be put in element's command stream.
        Sends acknowledge to caller and then runs command.
        Returns response with processed data to caller.
        """
        while not self._command_loop_shutdown.isSet():
            # Get oldest new command from element's command stream
            stream = {self._make_command_id(self.name): self.command_last_id}
            cmd_responses = self._rclient.xread(stream, block=MAX_BLOCK, count=1)
            if not cmd_responses:
                continue
            stream_name, msgs = cmd_responses[0]
            msg = msgs[0]  # we only read one
            cmd_id, cmd = msg
            # Set the command_last_id to this command's id to keep track of our last read
            self.command_last_id = cmd_id.decode()

            try:
                caller = cmd[b"element"].decode()
                cmd_name = cmd[b"cmd"].decode()
                data = cmd[b"data"]
            except KeyError:
                # Ignore non-commands
                continue

            if not caller:
                self.log(LogLevel.ERR, "No caller name present in command!")
                continue

            # Send acknowledge to caller
            if cmd_name not in self.timeouts.keys():
                timeout = RESPONSE_TIMEOUT
            else:
                timeout = self.timeouts[cmd_name]
            acknowledge = Acknowledge(self.name, cmd_id, timeout)
            _pipe = self._rpipeline_pool.get()
            _pipe.xadd(self._make_response_id(caller), vars(acknowledge), maxlen=STREAM_LEN)
            _pipe.execute()

            # Send response to caller
            if cmd_name not in self.handler_map.keys():
                self.log(LogLevel.ERR, "Received unsupported command.")
                response = Response(
                    err_code=ATOM_COMMAND_UNSUPPORTED, err_str="Unsupported command.")
            else:
                if cmd_name not in self.reserved_commands:
                    if "deserialize" in self.handler_map[cmd_name]:  # check for deprecated legacy mode
                        serialization = "msgpack" if self.handler_map[cmd_name]["deserialize"] else None
                    else:
                        serialization = self.handler_map[cmd_name]["serialization"]

                    data = ser.deserialize(data, method=serialization)
                    response = self.handler_map[cmd_name]["handler"](data)
                else:
                    # healthcheck/version requests/command_list commands don't care what data you are sending
                    response = self.handler_map[cmd_name]["handler"]()

                # Add ATOM_USER_ERRORS_BEGIN to err_code to map to element error range
                if isinstance(response, Response):
                    if response.err_code != 0:
                        response.err_code += ATOM_USER_ERRORS_BEGIN
                else:
                    response = Response(err_code=ATOM_CALLBACK_FAILED,
                                        err_str=f"Return type of {cmd_name} is not of type Response")

            kv = vars(response)
            kv["cmd_id"] = cmd_id
            kv["element"] = self.name
            kv["cmd"] = cmd_name
            _pipe.xadd(self._make_response_id(caller), kv, maxlen=STREAM_LEN)
            _pipe.execute()
            _pipe = self._release_pipeline(_pipe)

    # Triggers graceful exit of command loop
    def command_loop_shutdown(self):
        self._command_loop_shutdown.set()

    def command_send(self,
                     element_name,
                     cmd_name,
                     data="",
                     block=True,
                     ack_timeout=ACK_TIMEOUT,
                     serialization=None,
                     serialize=None,
                     deserialize=None):
        """
        Sends command to element and waits for acknowledge.
        When acknowledge is received, waits for timeout from acknowledge or until response is received.

        Args:
            element_name (str): Name of the element to send the command to.
            cmd_name (str): Name of the command to execute of element_name.
            data: Entry to be passed to the function specified by cmd_name.
            block (bool): Wait for the response before returning from the function.
            ack_timeout (int, optional): Time in milliseconds to wait for ack before
                                         timing out, overrides default value.
            serialization (str, optional): Method of serialization to use;
                                           defaults to None.

            Deprecated:
            serialize (bool, optional): Whether or not to serialize the data with msgpack
                                        before sending it to the command; defaults to None.
            deserialize (bool, optional): Whether or not to deserialize the data with
                                          msgpack in the response; defaults to None.

        Returns:
            A dictionary of the response from the command.
        """
        # cache the last response id at the time we are issuing this command, since this can get overwritten
        local_last_id = self.response_last_id
        timeout = None
        resp = None
        data = format_redis_py(data)

        # Send command to element's command stream
        if serialize is not None:  # check for deprecated legacy mode
            serialization = "msgpack" if serialize else None

        data = ser.serialize(data, method=serialization) if (data != "") else data

        cmd = Cmd(self.name, cmd_name, data)
        _pipe = self._rpipeline_pool.get()
        _pipe.xadd(self._make_command_id(element_name), vars(cmd), maxlen=STREAM_LEN)
        cmd_id = _pipe.execute()[-1].decode()
        _pipe = self._release_pipeline(_pipe)

        # Receive acknowledge from element
        # You have no guarantee that the response from the xread is for your specific thread,
        # so keep trying until we either receive our ack, or timeout is exceeded
        start_read = time.time()
        elapsed_time_ms = (time.time() - start_read) * 1000
        while True:
            responses = self._rclient.xread(
                {self._make_response_id(self.name): local_last_id},
                block=max(int(ack_timeout - elapsed_time_ms), 1)
            )
            if not responses:
                elapsed_time_ms = (time.time() - start_read) * 1000
                if elapsed_time_ms >= ack_timeout:
                    err_str = f"Did not receive acknowledge from {element_name}."
                    self.log(LogLevel.ERR, err_str)
                    return vars(Response(err_code=ATOM_COMMAND_NO_ACK, err_str=err_str))
                    break
                else:
                    continue

            stream, msgs = responses[0]  # we only read one stream
            for id, response in msgs:
                local_last_id = id.decode()

                if b"element" in response and response[b"element"].decode() == element_name \
                and b"cmd_id" in response and response[b"cmd_id"].decode() == cmd_id \
                and b"timeout" in response:
                    timeout = int(response[b"timeout"].decode())
                    break

                self._update_response_id_if_older(local_last_id)

            # If the response we received wasn't for this command, keep trying until ack timeout
            if timeout is not None:
                break

        if timeout is None:
            err_str = f"Did not receive acknowledge from {element_name}."
            self.log(LogLevel.ERR, err_str)
            return vars(Response(err_code=ATOM_COMMAND_NO_ACK, err_str=err_str))

        # Receive response from element
        # You have no guarantee that the response from the xread is for your specific thread,
        # so keep trying until we either receive our response, or timeout is exceeded
        start_read = time.time()
        while True:
            elapsed_time_ms = (time.time() - start_read) * 1000
            if elapsed_time_ms >= timeout:
                break

            responses = self._rclient.xread(
                {self._make_response_id(self.name): local_last_id},
                block=max(int(timeout - elapsed_time_ms), 1)
            )
            if not responses:
                err_str = f"Did not receive response from {element_name}."
                self.log(LogLevel.ERR, err_str)
                return vars(Response(err_code=ATOM_COMMAND_NO_RESPONSE, err_str=err_str))

            stream_name, msgs = responses[0]  # we only read from one stream
            for msg in msgs:
                id, response = msg
                local_last_id = id.decode()

                if b"element" in response and response[b"element"].decode() == element_name \
                and b"cmd_id" in response and response[b"cmd_id"].decode() == cmd_id \
                and b"err_code" in response:
                    err_code = int(response[b"err_code"].decode())
                    err_str = response[b"err_str"].decode() if b"err_str" in response else ""
                    if err_code != ATOM_NO_ERROR:
                        self.log(LogLevel.ERR, err_str)

                    response_data = response.get(b"data", "")
                    # check response for serialization method; if not present, use user specified method
                    if b"ser" in response:
                        serialization = response[b"ser"].decode()
                    elif deserialize is not None:  # check for deprecated legacy mode
                        serialization = "msgpack" if deserialize else None

                    try:
                        response_data = (ser.deserialize(response_data, method=serialization) if
                                         (len(response_data) != 0) else response_data)
                    except TypeError:
                        self.log(LogLevel.WARNING, "Could not deserialize response.")

                    # Make the final response
                    resp = vars(Response(data=response_data, err_code=err_code, err_str=err_str))
                    break

            self._update_response_id_if_older(local_last_id)
            if resp is not None:
                return resp

            # If the response we received wasn't for this command, keep trying until timeout
            continue

        # Proper response was not in responses
        err_str = f"Did not receive response from {element_name}."
        self.log(LogLevel.ERR, err_str)
        return vars(Response(err_code=ATOM_COMMAND_NO_RESPONSE, err_str=err_str))

    def entry_read_loop(self, stream_handlers, n_loops=None, timeout=MAX_BLOCK, serialization=None, force_serialization=False, deserialize=None):
        """
        Listens to streams and pass any received entry to corresponding handler.

        Args:
            stream_handlers (list of messages.StreamHandler):
            n_loops (int): Number of times to send the stream entry to the handlers.
            timeout (int): How long to block on the stream. If surpassed, the function returns.
            serialization (str, optional): If deserializing, the method of serialization
                                           to use; defaults to None.

            Deprecated:
            deserialize (bool, optional): Whether or not to deserialize the entries
                                          using msgpack; defaults to None.
        """
        if n_loops is None:
            # Create an infinite loop
            n_loops = iter(int, 1)
        else:
            n_loops = range(n_loops)

        streams = {}
        stream_handler_map = {}
        for stream_handler in stream_handlers:
            if not isinstance(stream_handler, StreamHandler):
                raise TypeError(f"{stream_handler} is not a StreamHandler!")
            stream_id = self._make_stream_id(stream_handler.element, stream_handler.stream)
            streams[stream_id] = self._get_redis_timestamp()
            stream_handler_map[stream_id] = stream_handler.handler
        for _ in n_loops:
            stream_entries = self._rclient.xread(streams, block=timeout)
            if not stream_entries:
                return
            for stream, msgs in stream_entries:
                for uid, entry in msgs:
                    streams[stream] = uid
                    entry = self._decode_entry(entry)
                    serialization = self._get_serialization_method(entry, serialization, force_serialization, deserialize)
                    entry = self._deserialize_entry(entry, method=serialization)
                    entry["id"] = uid.decode()
                    stream_handler_map[stream.decode()](entry)

    def entry_read_n(self, element_name, stream_name, n, serialization=None, force_serialization=False, deserialize=None):
        """
        Gets the n most recent entries from the specified stream.

        Args:
            element_name (str): Name of the element to get the entry from.
            stream_name (str): Name of the stream to get the entry from.
            n (int): Number of entries to get.
            serialization (str, optional): The method of deserialization to use;
                                           defaults to None.

            Deprecated:
            deserialize (bool, optional): Whether or not to deserialize the entries\
                                          using msgpack; defaults to None.

        Returns:
            List of dicts containing the data of the entries
        """
        entries = []
        stream_id = self._make_stream_id(element_name, stream_name)
        uid_entries = self._rclient.xrevrange(stream_id, count=n)
        for uid, entry in uid_entries:
            entry = self._decode_entry(entry)
            serialization = self._get_serialization_method(entry, serialization, force_serialization, deserialize)
            entry = self._deserialize_entry(entry, method=serialization)
            entry["id"] = uid.decode()
            entries.append(entry)

        return entries

    def entry_read_since(self,
                         element_name,
                         stream_name,
                         last_id="$",
                         n=None,
                         block=None,
                         serialization=None,
                         force_serialization=False,
                         deserialize=None):
        """
        Read entries from a stream since the last_id.

        Args:
            element_name (str): Name of the element to get the entry from.
            stream_name (str): Name of the stream to get the entry from.
            last_id (str, optional): Time from which to start get entries from. If '0', get all entries.
                If '$' (default), get only new entries after the function call (blocking).
            n (int, optional): Number of entries to get. If None, get all.
            block (int, optional): Time (ms) to block on the read. If 0, block forever.
                If None, don't block.
            serialization (str, optional): Method of deserialization to use;
                                           defaults to None.

            Deprecated:
            deserialize (bool, optional): Whether or not to deserialize the entries
                                          using msgpack; defaults to None.
        """
        streams, entries = {}, []
        stream_id = self._make_stream_id(element_name, stream_name)
        streams[stream_id] = last_id
        stream_entries = self._rclient.xread(streams, count=n, block=block)
        stream_names = [x[0].decode() for x in stream_entries]
        if not stream_entries or stream_id not in stream_names:
            return entries
        for stream_name, msgs in stream_entries:
            if stream_name.decode() == stream_id:
                for uid, entry in msgs:
                    entry = self._decode_entry(entry)
                    serialization = self._get_serialization_method(entry, serialization, force_serialization, deserialize)
                    entry = self._deserialize_entry(entry, method=serialization)
                    entry["id"] = uid.decode()
                    entries.append(entry)
        return entries

    def entry_write(self, stream_name, field_data_map, maxlen=STREAM_LEN, serialization=None, serialize=None):
        """
        Creates element's stream if it does not exist.
        Adds the fields and data to a Entry and puts it in the element's stream.

        Args:
            stream_name (str): The stream to add the data to.
            field_data_map (dict): Dict which creates the Entry. See messages.Entry for more usage.
            maxlen (int, optional): The maximum number of data to keep in the stream.
            serialization (str, optional): Method of serialization to use;
                                           defaults to None.

            Deprecated:
            serialize (bool, optional): Whether or not to serialize the entry using
                                        msgpack; defaults to None.

        Return: ID of item added to stream
        """
        self.streams.add(stream_name)
        field_data_map = format_redis_py(field_data_map)

        if serialize is not None:  # check for deprecated legacy mode
            serialization = "msgpack" if serialize else None

        ser_field_data_map = {}
        for k, v in field_data_map.items():
            if k in ENTRY_RESERVED_KEYS:
                raise ValueError(f"Invalid key \"{k}\": \"{k}\" is a reserved entry key")
            ser_field_data_map[k] = ser.serialize(v, method=serialization)

        ser_field_data_map["ser"] = str(serialization) if serialization is not None else "none"
        entry = Entry(ser_field_data_map)

        _pipe = self._rpipeline_pool.get()
        _pipe.xadd(self._make_stream_id(self.name, stream_name), vars(entry), maxlen=maxlen)
        ret = _pipe.execute()
        _pipe = self._release_pipeline(_pipe)

        if (type(ret) != list) or (len(ret) != 1) or (type(ret[0]) != bytes):
            print(ret)
            raise ValueError("Failed to write data to stream")

        return ret[0].decode()

    def log(self, level, msg, stdout=True):
        """
        Writes a message to log stream with loglevel.

        Args:
            level (messages.LogLevel): Unix syslog severity of message.
            message (str): The message to write for the log.
            stdout (bool, optional): Whether to write to stdout or only write to log stream.
        """
        log = Log(self.name, self.host, level, msg)
        _pipe = self._rpipeline_pool.get()
        _pipe.xadd("log", vars(log), maxlen=STREAM_LEN)
        _pipe.execute()
        _pipe = self._release_pipeline(_pipe)
        if stdout:
            print(msg)

    def reference_create(self, *data, serialization=None, serialize=None, timeout_ms=10000):
        """
        Creates one or more expiring references (similar to a pointer) in the atom system.
        This will typically be used when we've gotten a piece of data from a
        stream and we want it to persist past the length of time it would live
        in the stream s.t. we can pass it to other commands/elements. The
        references will simply be cached values in redis and will expire after
        the timeout_ms amount of time.

        Args:
            data (binary or object): one or more data items to be included in the reference
            timeout_ms (int, optional): How long the reference should persist in atom
                        unless otherwise extended/deleted. Set to 0 to have the
                        reference never time out (generally a terrible idea)
            serialization (str, optional): Method of serialization to use;
                                           defaults to None.

            Deprecated:
            serialize (bool, optional): whether or not to serialize the data using
                                        msgpack before creating the reference

        Return:
            List of references corresponding to the arguments passed
        """
        keys = []

        if serialize is not None:  # check for deprecated legacy mode
            serialization = "msgpack" if serialize else None

        _pipe = self._rpipeline_pool.get()
        px_val = timeout_ms if timeout_ms != 0 else None
        for datum in data:
            # Get the key name for the reference to use in redis
            key = self._make_reference_id()

            # Now, we can go ahead and do the SET in redis for the key
            # Expire as set by the user
            serialized_datum = ser.serialize(datum, method=serialization)
            key = key + ":ser:" + (str(serialization) if serialization is not None else "none")
            _pipe.set(key, serialized_datum, px=px_val, nx=True)

            keys.append(key)

        response = _pipe.execute()
        _pipe = self._release_pipeline(_pipe)

        if not all(response):
            raise ValueError(f"Failed to create reference! response {response}")

        # Return the key that was generated for the reference
        return keys

    def reference_create_from_stream(self, element, stream, stream_id="", timeout_ms=10000):
        """
        Creates an expiring reference (similar to a pointer) in the atom system.
        This API will take an element and a stream and, depending on the value
        of the stream_id field, will create a reference within Atom without
        the data ever having left Redis. This is optimal for performance and
        memory reasons. If the id arg is "" then we will make a reference
        from the most recent piece of data. If it is a particular ID we will
        make a reference from that piece of data.

        Since streams have multiple key:value pairs, one reference per key
        in the stream will be created, and the return type is a dictionary mapping
        stream keys to references.  The references are named so that the stream key
        is also included in the name of the corresponding reference.

        Args:

            element (string) : Name of the element whose stream we want to
                        make a reference from
            stream (string) : Stream from which we want to make a reference
            id (string) : If "", will use the most recent value from the
                        stream. Else, will try to make a reference from the
                        particular stream ID
            timeout_ms (int): How long the reference should persist in atom
                        unless otherwise extended/deleted. Set to 0 to have the
                        reference never time out (generally a terrible idea)

        Return:
            dictionary mapping stream keys to reference keys. Raises
            an error on failure.
        """

        if self._stream_reference_sha is None:
            raise ValueError("Lua script not loaded -- unable to call reference_create_from_stream")

        # Make the new reference key
        key = self._make_reference_id()

        # Get the stream we'll be reading from
        stream_name = self._make_stream_id(element, stream)

        # Call the script to make a reference
        _pipe = self._rpipeline_pool.get()
        _pipe.evalsha(self._stream_reference_sha, 0, stream_name, stream_id, key, timeout_ms)
        data = _pipe.execute()
        _pipe = self._release_pipeline(_pipe)

        if (type(data) != list) or (len(data) != 1) or (type(data[0]) != list):
            raise ValueError("Failed to make reference!")

        # Make a dictionary to return from the response
        key_dict = {}
        for key in data[0]:
            key_val = key.decode().split(':')[-1]
            key_dict[key_val] = key

        return key_dict

    def reference_get(self, *keys, serialization=None, force_serialization=False, deserialize=None):
        """
        Gets one or more reference from the atom system. Reads the key(s) from redis
        and returns the data, performing a serialize/deserialize operation on each
        key as commanded by the user

        Args:
            keys (str): One or more keys of references to get from Atom
            serialization (str, optional): If deserializing, the method of serialization to use; defaults to msgpack.

            Deprecated:
            deserialize (bool, optional): Whether or not to deserialize reference; defaults to False.
        Return:
            List of items corresponding to each reference key passed as an argument
        """

        # Get the data
        _pipe = self._rpipeline_pool.get()
        for key in keys:
            _pipe.get(key)
        data = _pipe.execute()
        _pipe = self._release_pipeline(_pipe)

        if type(data) is not list:
            raise ValueError(f"Invalid response from redis: {data}")

        deserialized_data = [ ]
        for key, ref in zip(keys, data):
            # look for serialization method in reference key first; if not present use user specified method
            key_split = key.split(':') if type(key) == str else key.decode().split(':')

            # Need to reformat the data into a dictionary with a "ser"
            #   key like it comes in on entries to use the shared logic function
            get_serialization_data = {}
            if "ser" in key_split:
                get_serialization_data["ser"] = key_split[key_split.index("ser") + 1]

            # Use the serialization data to get the method for deserializing
            #   according to the user's preference
            serialization = self._get_serialization_method(
                get_serialization_data,
                serialization,
                force_serialization,
                deserialize
            )

            # Deserialize the data
            deserialized_data.append(ser.deserialize(ref, method=serialization) if ref is not None else None)

        return deserialized_data

    def reference_delete(self, *keys):
        """
        Deletes one or more references and cleans up their memory

        Args:
            keys (strs): Keys of references to delete from Atom
        """

        # Unlink the data
        _pipe = self._rpipeline_pool.get()
        for key in keys:
            _pipe.delete(key)
        data = _pipe.execute()
        _pipe = self._release_pipeline(_pipe)

        if type(data) is not list:
            raise ValueError(f"Invalid response from redis: {data}")
        if all(data) != 1:
            raise KeyError(f"Reference {key} not in redis")

    def reference_update_timeout_ms(self, key, timeout_ms):
        """
        Updates the timeout for an existing reference. This might want to
        be done as we won't know exactly how long we'll need the key for
        at the original point in time for which we created it

        Args:
            key (str): Key of a reference for which we want to update the
                        timeout
            timeout_ms (int): Timeout at which we want the key to expire.
                        Pass <= 0 for no timeout, i.e. never expire (generally
                        a terrible idea)

        """
        _pipe = self._rpipeline_pool.get()

        # Call pexpeire to set the timeout in ms if we got a positive
        #   nonzero timeout, else call persist to remove any existing
        #   timeout
        if timeout_ms > 0:
            _pipe.pexpire(key, timeout_ms)
        else:
            _pipe.persist(key)

        data = _pipe.execute()
        _pipe = self._release_pipeline(_pipe)

        # Make sure there's only one value in the data return
        if type(data) != list and len(data) != 1:
            raise ValueError(f"Invalid response from redis: {data}")

        if data[0] != 1:
            raise KeyError(f"Reference {key} not in redis")

    def reference_get_timeout_ms(self, key):
        """
        Get the current amount of ms left on the reference. Mainly useful
        for debug I'd imagine. Returns -1 if no timeout, else the timeout
        in ms.

        Args:
            key (str):  Key of a reference for which we want to get the
                        timeout ms for.
        """
        _pipe = self._rpipeline_pool.get()
        _pipe.pttl(key)
        data = _pipe.execute()
        _pipe = self._release_pipeline(_pipe)

        if type(data) != list and len(data) != 1:
            raise ValueError(f"Invalid response from redis: {data}")

        if data[0] == -2:
            raise KeyError(f"Reference {key} doesn't exist")

        return data[0]
