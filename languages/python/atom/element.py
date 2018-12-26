import redis
from atom.config import DEFAULT_REDIS_PORT, DEFAULT_REDIS_SOCKET
from atom.config import LANG, VERSION, ACK_TIMEOUT, RESPONSE_TIMEOUT, STREAM_LEN, MAX_BLOCK
from atom.config import ATOM_NO_ERROR, ATOM_COMMAND_NO_ACK, ATOM_COMMAND_NO_RESPONSE
from atom.config import ATOM_COMMAND_UNSUPPORTED, ATOM_CALLBACK_FAILED, ATOM_USER_ERRORS_BEGIN
from atom.messages import Cmd, Response, StreamHandler
from atom.messages import Acknowledge, Entry, Response, Log, LogLevel
from msgpack import packb, unpackb
from os import uname
from sys import exit


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
        try:
            if host is not None:
                self._rclient = redis.StrictRedis(host=host, port=port)
            else:
                self._rclient = redis.StrictRedis(unix_socket_path=socket_path)
            self._pipe = self._rclient.pipeline()
            self._pipe.xadd(
                self._make_response_id(self.name),
                maxlen=STREAM_LEN,
                **{
                    "language": LANG,
                    "version": VERSION
                })
            # Keep track of response_last_id to know last time the client's response stream was read from
            self.response_last_id = self._pipe.execute()[-1].decode()

            self._pipe.xadd(
                self._make_command_id(self.name),
                maxlen=STREAM_LEN,
                **{
                    "language": LANG,
                    "version": VERSION
                })
            # Keep track of command_last_id to know last time the element's command stream was read from
            self.command_last_id = self._pipe.execute()[-1].decode()

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

    def _get_redis_timestamp(self):
        """
        Gets the current timestamp from Redis.
        """
        secs, msecs = self._rclient.time()
        timestamp = str(secs) + str(msecs).zfill(6)[:3]
        return timestamp

    def _decode_entry(self, entry):
        """
        Decodes the binary keys of an entry and the binary timestamp.
        Leaves the values of non-timestamp fields untouched as they may be intentionally binary.

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
            if k_str == "timestamp":
                decoded_entry[k_str] = entry[k].decode()
            else:
                decoded_entry[k_str] = entry[k]
        return decoded_entry

    def _deserialize_entry(self, entry):
        """
        Deserializes the binary data of the entry and puts the keys and fields of the data into the entry.

        Args:
            entry (dict): The entry in dictionary form to deserialize with data as key 'bin_data'.
        Returns:
            The deserialized entry as a dictionary.
        """
        try:
            data = unpackb(entry["bin_data"], raw=False)
            del entry["bin_data"]
        except TypeError or KeyError:
            raise TypeError("Received data not serialized by atom! Cannot deserialize.")
        for k, v in data.items():
            entry[k] = v
        return entry

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

    def command_add(self, name, handler, timeout=RESPONSE_TIMEOUT, deserialize=False):
        """
        Adds a command to the element for another element to call.

        Args:
            name (str): Name of the command.
            handler (callable): Function to call given the command name.
            timeout (int, optional): Time for the caller to wait for the command to finish.
            deserialize (bool, optional): Whether or not to deserialize the data using msgpack before passing it to the handler.
        """
        if not callable(handler):
            raise TypeError("Passed in handler is not a function!")
        self.handler_map[name] = {"handler": handler, "deserialize": deserialize}
        self.timeouts[name] = timeout

    def command_loop(self):
        """
        Waits for command to be put in element's command stream.
        Sends acknowledge to caller and then runs command.
        Returns response with processed data to caller.
        """
        while True:
            # Get oldest new command from element's command stream
            stream = {self._make_command_id(self.name): self.command_last_id}
            cmd_response = self._rclient.xread(block=MAX_BLOCK, count=1, **stream)
            if cmd_response is None:
                continue

            # Set the command_last_id to this command's id to keep track of our last read
            cmd_id, cmd = cmd_response[self._make_command_id(self.name)][0]
            self.command_last_id = cmd_id

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
            self._pipe.xadd(self._make_response_id(caller), maxlen=STREAM_LEN, **vars(acknowledge))
            self._pipe.execute()

            # Send response to caller
            if cmd_name not in self.handler_map.keys():
                self.log(LogLevel.ERR, "Received unsupported command.")
                response = Response(
                    err_code=ATOM_COMMAND_UNSUPPORTED, err_str="Unsupported command.")
            else:
                try:
                    data = unpackb(data, raw=False) if self.handler_map[cmd_name]["deserialize"] else data
                    response = self.handler_map[cmd_name]["handler"](data)
                    if not isinstance(response, Response):
                        raise TypeError(f"Return type of {cmd_name} is not of type Response")
                    # Add ATOM_USER_ERRORS_BEGIN to err_code to map to element error range
                    if response.err_code != 0:
                        response.err_code += ATOM_USER_ERRORS_BEGIN
                except Exception as e:
                    err_str = f"{str(type(e))} {str(e)}"
                    self.log(LogLevel.ERR, err_str)
                    response = Response(err_code=ATOM_CALLBACK_FAILED, err_str=err_str)

            response = response.to_internal(self.name, cmd_name, cmd_id)
            self._pipe.xadd(self._make_response_id(caller), maxlen=STREAM_LEN, **vars(response))
            self._pipe.execute()

    def command_send(self, element_name, cmd_name, data, block=True, serialize=False, deserialize=False):
        """
        Sends command to element and waits for acknowledge.
        When acknowledge is received, waits for timeout from acknowledge or until response is received.

        Args:
            element_name (str): Name of the element to send the command to.
            cmd_name (str): Name of the command to execute of element_name.
            data: Entry to be passed to the function specified by cmd_name.
            block (bool): Wait for the response before returning from the function.
            serialize (bool, optional): Whether or not to serialize the data using msgpack before sending it to the command.
            deserialize (bool, optional): Whether or not to deserialize the data in the response using msgpack.

        Returns:
            A dictionary of the response from the command.
        """
        # Send command to element's command stream
        data = packb(data, use_bin_type=True) if serialize else data
        cmd = Cmd(self.name, cmd_name, data)
        self._pipe.xadd(self._make_command_id(element_name), maxlen=STREAM_LEN, **vars(cmd))
        cmd_id = self._pipe.execute()[-1].decode()
        timeout = None

        # Receive acknowledge from element
        responses = self._rclient.xread(
            block=ACK_TIMEOUT, **{self._make_response_id(self.name): self.response_last_id})
        if responses is None:
            err_str = f"Did not receive acknowledge from {element_name}."
            self.log(LogLevel.ERR, err_str)
            return vars(Response(err_code=ATOM_COMMAND_NO_ACK, err_str=err_str))
        for self.response_last_id, response in responses[self._make_response_id(self.name)]:
            if response[b"element"].decode() == element_name and \
            response[b"cmd_id"].decode() == cmd_id and b"timeout" in response:
                timeout = int(response[b"timeout"].decode())
                break

        if timeout is None:
            err_str = f"Did not receive acknowledge from {element_name}."
            self.log(LogLevel.ERR, err_str)
            return vars(Response(err_code=ATOM_COMMAND_NO_ACK, err_str=err_str))

        # Receive response from element
        responses = self._rclient.xread(
            block=timeout, **{self._make_response_id(self.name): self.response_last_id})
        if responses is None:
            err_str = f"Did not receive response from {element_name}."
            self.log(LogLevel.ERR, err_str)
            return vars(Response(err_code=ATOM_COMMAND_NO_RESPONSE, err_str=err_str))
        for self.response_last_id, response in responses[self._make_response_id(self.name)]:
            if response[b"element"].decode() == element_name and \
            response[b"cmd_id"].decode() == cmd_id:
                err_code = int(response[b"err_code"].decode())
                err_str = response[b"err_str"].decode() if b"err_str" in response else ""
                if err_code != ATOM_NO_ERROR:
                    self.log(LogLevel.ERR, err_str)
                response_data = response.get(b"data", "")
                try:
                    response_data = unpackb(response_data, raw=False) if deserialize else response_data
                except TypeError:
                    self.log(LogLevel.WARNING, "Could not deserialize response.")
                return vars(Response(data=response_data, err_code=err_code, err_str=err_str))

        # Proper response was not in responses
        err_str = f"Did not receive response from {element_name}."
        self.log(LogLevel.ERR, err_str)
        return vars(Response(err_code=ATOM_COMMAND_NO_RESPONSE, err_str=err_str))

    def entry_read_loop(self, stream_handlers, n_loops=None, timeout=MAX_BLOCK, deserialize=False):
        """
        Listens to streams and pass any received entry to corresponding handler.

        Args:
            stream_handlers (list of messages.StreamHandler):
            n_loops (int): Number of times to send the stream entry to the handlers.
            timeout (int): How long to block on the stream. If surpassed, the function returns.
            deserialize (bool, optional): Whether or not to deserialize the entries using msgpack.
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
            stream_entries = self._rclient.xread(block=timeout, **streams)
            if stream_entries is None:
                return
            for stream, uid_entries in stream_entries.items():
                for uid, entry in uid_entries:
                    streams[stream] = uid
                    entry = self._decode_entry(entry)
                    entry = self._deserialize_entry(entry) if deserialize else entry
                    if "timestamp" not in entry or not entry["timestamp"]:
                        entry["timestamp"] = uid.decode()
                    stream_handler_map[stream](entry)

    def entry_read_n(self, element_name, stream_name, n, deserialize=False):
        """
        Gets the n most recent entries from the specified stream.

        Args:
            element_name (str): Name of the element to get the entry from.
            stream_name (str): Name of the stream to get the entry from.
            n (int): Number of entries to get.
            deserialize (bool, optional): Whether or not to deserialize the entries using msgpack.

        Returns:
            List of dicts containing the data of the entries
        """
        entries = []
        stream_id = self._make_stream_id(element_name, stream_name)
        uid_entries = self._rclient.xrevrange(stream_id, count=n)
        for uid, entry in uid_entries:
            entry = self._decode_entry(entry)
            entry = self._deserialize_entry(entry) if deserialize else entry
            if "timestamp" not in entry or not entry["timestamp"]:
                entry["timestamp"] = uid.decode()
            entries.append(entry)
        return entries

    def entry_read_since(self, element_name, stream_name, last_id="0", n=None, block=None, deserialize=False):
        """
        Read entries from a stream since the last_id.

        Args:
            element_name (str): Name of the element to get the entry from.
            stream_name (str): Name of the stream to get the entry from.
            last_id (str, optional): Time from which to start get entries from. If '0', get all entries.
            n (int, optional): Number of entries to get. If None, get all.
            block (int, optional): Time (ms) to block on the read. If None, don't block.
            deserialize (bool, optional): Whether or not to deserialize the entries using msgpack.
        """
        streams, entries = {}, []
        stream_id = self._make_stream_id(element_name, stream_name)
        streams[stream_id] = last_id
        stream_entries = self._rclient.xread(count=n, block=block, **streams)
        if not stream_entries or stream_id not in stream_entries:
            return entries
        for uid, entry in stream_entries[stream_id]:
            entry = self._decode_entry(entry)
            entry = self._deserialize_entry(entry) if deserialize else entry
            if "timestamp" not in entry or not entry["timestamp"]:
                entry["timestamp"] = uid.decode()
            entries.append(entry)
        return entries

    def entry_write(self, stream_name, field_data_map, timestamp="", maxlen=STREAM_LEN, serialize=False):
        """
        Creates element's stream if it does not exist.
        Adds the fields and data to a Entry and puts it in the element's stream.

        Args:
            stream_name (str): The stream to add the data to.
            field_data_map (dict): Dict which creates the Entry. See messages.Entry for more usage.
            timestamp (str, optional): Timestamp of when the data was created.
            maxlen (int, optional): The maximum number of data to keep in the stream.
            serialize (bool, optional): Whether or not to serialize the entry using msgpack.
        """
        self.streams.add(stream_name)
        entry = Entry(field_data_map, timestamp)
        if serialize:
            entryb = packb(vars(entry), use_bin_type=True)
            self._pipe.xadd(
                self._make_stream_id(self.name, stream_name), maxlen=maxlen, bin_data=entryb)
        else:
            self._pipe.xadd(
                self._make_stream_id(self.name, stream_name), maxlen=maxlen, **vars(entry))
        self._pipe.execute()

    def log(self, level, msg, stdout=True):
        """
        Writes a message to log stream with loglevel.

        Args:
            level (messages.LogLevel): Unix syslog severity of message.
            message (str): The message to write for the log.
            stdout (bool, optional): Whether to write to stdout or only write to log stream.
        """
        log = Log(self.name, self.host, level, msg)
        self._pipe.xadd("log", maxlen=STREAM_LEN, **vars(log))
        self._pipe.execute()
        if stdout:
            print(msg)
