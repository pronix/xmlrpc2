from __future__ import absolute_import
from __future__ import division

import base64
import cgi
import datetime
import errno
import socket
import time

from io import BytesIO
from xml.parsers import expat

try:
    import gzip
except ImportError:
    gzip = None  # python can be built without zlib/gzip support


from . import __version__
from .constants import MAXINT, MININT
from .exceptions import UnsupportedScheme
from .exceptions import ProtocolError, ResponseError, Fault


from .compat import is_py2
from .compat import UnicodeMixin, httplib, urllib_parse, basestring, bytes, str


##
# Wrapper for XML-RPC DateTime values.  This converts a time value to
# the format used by XML-RPC.
# <p>
# The value can be given as a string in the format
# "yyyymmddThh:mm:ss", as a 9-item time tuple (as returned by
# time.localtime()), or an integer value (as returned by time.time()).
# The wrapper uses time.localtime() to convert an integer to a time
# tuple.
#
# @param value The time, given as an ISO 8601 string, a time
#              tuple, or a integer time value.


def _strftime(value):
    if isinstance(value, datetime.datetime):
        return "%04d%02d%02dT%02d:%02d:%02d" % (
            value.year, value.month, value.day,
            value.hour, value.minute, value.second)

    if not isinstance(value, (tuple, time.struct_time)):
        if value == 0:
            value = time.time()
        value = time.localtime(value)

    return "%04d%02d%02dT%02d:%02d:%02d" % value[:6]


class DateTime:
    """DateTime wrapper for an ISO 8601 string or time tuple or
    localtime integer value to generate 'dateTime.iso8601' XML-RPC
    value.
    """

    def __init__(self, value=0):
        if isinstance(value, basestring):
            self.value = value
        else:
            self.value = _strftime(value)

    def make_comparable(self, other):
        if isinstance(other, DateTime):
            s = self.value
            o = other.value
        elif isinstance(other, datetime.datetime):
            s = self.value
            o = other.strftime("%Y%m%dT%H:%M:%S")
        elif isinstance(other, str):
            s = self.value
            o = other
        elif isinstance(other, bytes) and is_py2:
            s = self.value
            o = other
        elif hasattr(other, "timetuple"):
            s = self.timetuple()
            o = other.timetuple()
        else:
            otype = (hasattr(other, "__class__")
                     and other.__class__.__name__
                     or type(other))
            raise TypeError("Can't compare %s and %s" %
                            (self.__class__.__name__, otype))
        return s, o

    def __lt__(self, other):
        s, o = self.make_comparable(other)
        return s < o

    def __le__(self, other):
        s, o = self.make_comparable(other)
        return s <= o

    def __gt__(self, other):
        s, o = self.make_comparable(other)
        return s > o

    def __ge__(self, other):
        s, o = self.make_comparable(other)
        return s >= o

    def __eq__(self, other):
        s, o = self.make_comparable(other)
        return s == o

    def __ne__(self, other):
        s, o = self.make_comparable(other)
        return s != o

    def timetuple(self):
        return time.strptime(self.value, "%Y%m%dT%H:%M:%S")

    ##
    # Get date/time value.
    #
    # @return Date/time value, as an ISO 8601 string.

    def __str__(self):
        return self.value

    def __repr__(self):
        return "<DateTime %r at %x>" % (self.value, id(self))

    def decode(self, data):
        self.value = str(data).strip()

    def encode(self, out):
        out.write("<value><dateTime.iso8601>")
        out.write(self.value)
        out.write("</dateTime.iso8601></value>\n")


def _datetime(data):
    # decode xml element contents into a DateTime structure.
    value = DateTime()
    value.decode(data)
    return value


def _datetime_type(data):
    t = time.strptime(data, "%Y%m%dT%H:%M:%S")
    return datetime.datetime(*tuple(t)[:6])

##
# Wrapper for binary data.  This can be used to transport any kind
# of binary data over XML-RPC, using BASE64 encoding.
#
# @param data An 8-bit string containing arbitrary data.


class Binary:
    """Wrapper for binary data."""

    def __init__(self, data=None):
        if data is None:
            data = b""
        else:
            if not isinstance(data, bytes):
                raise TypeError("expected bytes, not %s" %
                                data.__class__.__name__)
            data = bytes(data)  # Make a copy of the bytes!
        self.data = data

    ##
    # Get buffer contents.
    #
    # @return Buffer contents, as an 8-bit string.

    def __str__(self):
        return str(self.data, "latin-1")  # XXX encoding?!

    def __eq__(self, other):
        if isinstance(other, Binary):
            other = other.data
        return self.data == other

    def __ne__(self, other):
        if isinstance(other, Binary):
            other = other.data
        return self.data != other

    def decode(self, data):
        self.data = base64.decodebytes(data)

    def encode(self, out):
        out.write("<value><base64>\n")
        encoded = base64.encodebytes(self.data)
        out.write(encoded.decode('ascii'))
        out.write('\n')
        out.write("</base64></value>\n")


def _binary(data):
    # decode xml element contents into a Binary structure
    value = Binary()
    value.decode(data)
    return value

WRAPPERS = (DateTime, Binary)

# --------------------------------------------------------------------
# XML parsers


class ExpatParser:
    # fast expat parser for Python 2.0 and later.
    def __init__(self, target):
        self._parser = parser = expat.ParserCreate(None, None)
        self._target = target
        parser.StartElementHandler = target.start
        parser.EndElementHandler = target.end
        parser.CharacterDataHandler = target.data
        encoding = None
        target.xml(encoding, None)

    def feed(self, data):
        self._parser.Parse(data, 0)

    def close(self):
        self._parser.Parse("", 1)  # end of data
        del self._target, self._parser  # get rid of circular references

# --------------------------------------------------------------------
# XML-RPC marshalling and unmarshalling code

##
# XML-RPC marshaller.
#
# @param encoding Default encoding for 8-bit strings.  The default
#     value is None (interpreted as UTF-8).
# @see dumps


class Marshaller:
    """Generate an XML-RPC params chunk from a Python data structure.

    Create a Marshaller instance for each set of parameters, and use
    the "dumps" method to convert your data (represented as a tuple)
    to an XML-RPC params chunk.  To write a fault response, pass a
    Fault instance instead.  You may prefer to use the "dumps" module
    function for this purpose.
    """

    # by the way, if you don't understand what's going on in here,
    # that's perfectly ok.

    def __init__(self, encoding=None, allow_none=False):
        self.memo = {}
        self.data = None
        self.encoding = encoding
        self.allow_none = allow_none

    dispatch = {}

    def dumps(self, values):
        out = []
        write = out.append
        dump = self.__dump
        if isinstance(values, Fault):
            # fault instance
            write("<fault>\n")
            dump({'faultCode': values.faultCode,
                  'faultString': values.faultString},
                 write)
            write("</fault>\n")
        else:
            # parameter block
            # FIXME: the xml-rpc specification allows us to leave out
            # the entire <params> block if there are no parameters.
            # however, changing this may break older code (including
            # old versions of xmlrpclib.py), so this is better left as
            # is for now.  See @XMLRPC3 for more information. /F
            write("<params>\n")
            for v in values:
                write("<param>\n")
                dump(v, write)
                write("</param>\n")
            write("</params>\n")
        result = "".join(out)
        return result

    def __dump(self, value, write):
        try:
            f = self.dispatch[type(value)]
        except KeyError:
            # check if this object can be marshalled as a structure
            if not hasattr(value, '__dict__'):
                raise TypeError("cannot marshal %s objects" % type(value))
            # check if this class is a sub-class of a basic type,
            # because we don't know how to marshal these types
            # (e.g. a string sub-class)
            for type_ in type(value).__mro__:
                if type_ in self.dispatch.keys():
                    raise TypeError("cannot marshal %s objects" % type(value))
            # XXX(twouters): using "_arbitrary_instance" as key as a quick-fix
            # for the p3yk merge, this should probably be fixed more neatly.
            f = self.dispatch["_arbitrary_instance"]
        f(self, value, write)

    def dump_nil(self, value, write):
        if not self.allow_none:
            raise TypeError("cannot marshal None unless allow_none is enabled")
        write("<value><nil/></value>")
    dispatch[type(None)] = dump_nil

    def dump_int(self, value, write):
        # in case ints are > 32 bits
        if value > MAXINT or value < MININT:
            raise OverflowError("int exceeds XML-RPC limits")
        write("<value><int>")
        write(str(value))
        write("</int></value>\n")

    def dump_long(self, value, write):
        if value > MAXINT or value < MININT:
            raise OverflowError("long int exceeds XML-RPC limits")
        write("<value><int>")
        write(str(int(value)))
        write("</int></value>\n")

    if is_py2:
        dispatch[long] = dump_long
        dispatch[int] = dump_int
    else:
        dispatch[int] = dump_long

    def dump_bool(self, value, write):
        write("<value><boolean>")
        write(value and "1" or "0")
        write("</boolean></value>\n")
    dispatch[bool] = dump_bool

    def dump_double(self, value, write):
        write("<value><double>")
        write(repr(value))
        write("</double></value>\n")
    dispatch[float] = dump_double

    def dump_unicode(self, value, write, escape=cgi.escape):
        write("<value><string>")
        write(escape(value))
        write("</string></value>\n")
    dispatch[str] = dump_unicode

    if is_py2:
        dispatch[bytes] = dump_unicode

    def dump_array(self, value, write):
        i = id(value)
        if i in self.memo:
            raise TypeError("cannot marshal recursive sequences")
        self.memo[i] = None
        dump = self.__dump
        write("<value><array><data>\n")
        for v in value:
            dump(v, write)
        write("</data></array></value>\n")
        del self.memo[i]
    dispatch[tuple] = dump_array
    dispatch[list] = dump_array

    def dump_struct(self, value, write, escape=cgi.escape):
        i = id(value)
        if i in self.memo:
            raise TypeError("cannot marshal recursive dictionaries")
        self.memo[i] = None
        dump = self.__dump
        write("<value><struct>\n")
        for k, v in value.items():
            write("<member>\n")
            if not isinstance(k, basestring):
                raise TypeError("dictionary key must be string")
            write("<name>%s</name>\n" % escape(k))
            dump(v, write)
            write("</member>\n")
        write("</struct></value>\n")
        del self.memo[i]
    dispatch[dict] = dump_struct

    def dump_datetime(self, value, write):
        write("<value><dateTime.iso8601>")
        write(_strftime(value))
        write("</dateTime.iso8601></value>\n")
    dispatch[datetime.datetime] = dump_datetime

    def dump_instance(self, value, write):
        # check for special wrappers
        if value.__class__ in WRAPPERS:
            self.write = write
            value.encode(self)
            del self.write
        else:
            # store instance attributes as a struct (really?)
            self.dump_struct(value.__dict__, write)
    dispatch[DateTime] = dump_instance
    dispatch[Binary] = dump_instance
    # XXX(twouters): using "_arbitrary_instance" as key as a quick-fix
    # for the p3yk merge, this should probably be fixed more neatly.
    dispatch["_arbitrary_instance"] = dump_instance

##
# XML-RPC unmarshaller.
#
# @see loads


class Unmarshaller:
    """Unmarshal an XML-RPC response, based on incoming XML event
    messages (start, data, end).  Call close() to get the resulting
    data structure.

    Note that this reader is fairly tolerant, and gladly accepts bogus
    XML-RPC data without complaining (but not bogus XML).
    """

    # and again, if you don't understand what's going on in here,
    # that's perfectly ok.

    def __init__(self, use_datetime=False):
        self._type = None
        self._stack = []
        self._marks = []
        self._data = []
        self._methodname = None
        self._encoding = "utf-8"
        self.append = self._stack.append
        self._use_datetime = use_datetime
        if use_datetime and not datetime:
            raise ValueError("the datetime module is not available")

    def close(self):
        # return response tuple and target method
        if self._type is None or self._marks:
            raise ResponseError()
        if self._type == "fault":
            raise Fault(**self._stack[0])
        return tuple(self._stack)

    def getmethodname(self):
        return self._methodname

    #
    # event handlers

    def xml(self, encoding, standalone):
        self._encoding = encoding
        # FIXME: assert standalone == 1 ???

    def start(self, tag, attrs):
        # prepare to handle this element
        if tag == "array" or tag == "struct":
            self._marks.append(len(self._stack))
        self._data = []
        self._value = (tag == "value")

    def data(self, text):
        self._data.append(text)

    def end(self, tag):
        # call the appropriate end tag handler
        try:
            f = self.dispatch[tag]
        except KeyError:
            pass  # unknown tag ?
        else:
            return f(self, "".join(self._data))

    #
    # accelerator support

    def end_dispatch(self, tag, data):
        # dispatch data
        try:
            f = self.dispatch[tag]
        except KeyError:
            pass  # unknown tag ?
        else:
            return f(self, data)

    #
    # element decoders

    dispatch = {}

    def end_nil(self, data):
        self.append(None)
        self._value = 0
    dispatch["nil"] = end_nil

    def end_boolean(self, data):
        if data == "0":
            self.append(False)
        elif data == "1":
            self.append(True)
        else:
            raise TypeError("bad boolean value")
        self._value = 0
    dispatch["boolean"] = end_boolean

    def end_int(self, data):
        self.append(int(data))
        self._value = 0
    dispatch["i4"] = end_int
    dispatch["i8"] = end_int
    dispatch["int"] = end_int

    def end_double(self, data):
        self.append(float(data))
        self._value = 0
    dispatch["double"] = end_double

    def end_string(self, data):
        if self._encoding:
            data = data.decode(self._encoding)
        self.append(data)
        self._value = 0
    dispatch["string"] = end_string
    dispatch["name"] = end_string  # struct keys are always strings

    def end_array(self, data):
        mark = self._marks.pop()
        # map arrays to Python lists
        self._stack[mark:] = [self._stack[mark:]]
        self._value = 0
    dispatch["array"] = end_array

    def end_struct(self, data):
        mark = self._marks.pop()
        # map structs to Python dictionaries
        dict = {}
        items = self._stack[mark:]
        for i in range(0, len(items), 2):
            dict[items[i]] = items[i + 1]
        self._stack[mark:] = [dict]
        self._value = 0
    dispatch["struct"] = end_struct

    def end_base64(self, data):
        value = Binary()
        value.decode(data.encode("ascii"))
        self.append(value)
        self._value = 0
    dispatch["base64"] = end_base64

    def end_dateTime(self, data):
        value = DateTime()
        value.decode(data)
        if self._use_datetime:
            value = _datetime_type(data)
        self.append(value)
    dispatch["dateTime.iso8601"] = end_dateTime

    def end_value(self, data):
        # if we stumble upon a value element with no internal
        # elements, treat it as a string element
        if self._value:
            self.end_string(data)
    dispatch["value"] = end_value

    def end_params(self, data):
        self._type = "params"
    dispatch["params"] = end_params

    def end_fault(self, data):
        self._type = "fault"
    dispatch["fault"] = end_fault

    def end_methodName(self, data):
        if self._encoding:
            data = data.decode(self._encoding)
        self._methodname = data
        self._type = "methodName"  # no params
    dispatch["methodName"] = end_methodName

## Multicall support
#


class _MultiCallMethod:
    # some lesser magic to store calls made to a MultiCall object
    # for batch execution
    def __init__(self, call_list, name):
        self.__call_list = call_list
        self.__name = name

    def __getattr__(self, name):
        return _MultiCallMethod(self.__call_list, "%s.%s" % (self.__name, name))

    def __call__(self, *args):
        self.__call_list.append((self.__name, args))


class MultiCallIterator:
    """Iterates over the results of a multicall. Exceptions are
    thrown in response to xmlrpc faults."""

    def __init__(self, results):
        self.results = results

    def __getitem__(self, i):
        item = self.results[i]
        if type(item) == type({}):
            raise Fault(item['faultCode'], item['faultString'])
        elif type(item) == type([]):
            return item[0]
        else:
            raise ValueError("unexpected type in multicall result")


class MultiCall:
    """server -> a object used to boxcar method calls

    server should be a ServerProxy object.

    Methods can be added to the MultiCall using normal
    method call syntax e.g.:

    multicall = MultiCall(server_proxy)
    multicall.add(2,3)
    multicall.get_address("Guido")

    To execute the multicall, call the MultiCall object e.g.:

    add_result, address = multicall()
    """

    def __init__(self, server):
        self.__server = server
        self.__call_list = []

    def __repr__(self):
        return "<MultiCall at %x>" % id(self)

    __str__ = __repr__

    def __getattr__(self, name):
        return _MultiCallMethod(self.__call_list, name)

    def __call__(self):
        marshalled_list = []
        for name, args in self.__call_list:
            marshalled_list.append({'methodName': name, 'params': args})

        return MultiCallIterator(self.__server.system.multicall(marshalled_list))

# --------------------------------------------------------------------
# convenience functions

FastMarshaller = FastParser = FastUnmarshaller = None

##
# Create a parser object, and connect it to an unmarshalling instance.
# This function picks the fastest available XML parser.
#
# return A (parser, unmarshaller) tuple.


def getparser(use_datetime=False):
    """getparser() -> parser, unmarshaller

    Create an instance of the fastest available parser, and attach it
    to an unmarshalling object.  Return both objects.
    """
    if use_datetime and not datetime:
        raise ValueError("the datetime module is not available")
    if FastParser and FastUnmarshaller:
        if use_datetime:
            mkdatetime = _datetime_type
        else:
            mkdatetime = _datetime
        target = FastUnmarshaller(True, False, _binary, mkdatetime, Fault)
        parser = FastParser(target)
    else:
        target = Unmarshaller(use_datetime=use_datetime)
        if FastParser:
            parser = FastParser(target)
        else:
            parser = ExpatParser(target)
    return parser, target

##
# Convert a Python tuple or a Fault instance to an XML-RPC packet.
#
# @def dumps(params, **options)
# @param params A tuple or Fault instance.
# @keyparam methodname If given, create a methodCall request for
#     this method name.
# @keyparam methodresponse If given, create a methodResponse packet.
#     If used with a tuple, the tuple must be a singleton (that is,
#     it must contain exactly one element).
# @keyparam encoding The packet encoding.
# @return A string containing marshalled data.


def dumps(params, methodname=None, methodresponse=None, encoding=None,
          allow_none=False):
    """data [,options] -> marshalled data

    Convert an argument tuple or a Fault instance to an XML-RPC
    request (or response, if the methodresponse option is used).

    In addition to the data object, the following options can be given
    as keyword arguments:

        methodname: the method name for a methodCall packet

        methodresponse: true to create a methodResponse packet.
        If this option is used with a tuple, the tuple must be
        a singleton (i.e. it can contain only one element).

        encoding: the packet encoding (default is UTF-8)

    All 8-bit strings in the data structure are assumed to use the
    packet encoding.  Unicode strings are automatically converted,
    where necessary.
    """

    assert isinstance(params, (tuple, Fault)), "argument must be tuple or Fault instance"
    if isinstance(params, Fault):
        methodresponse = 1
    elif methodresponse and isinstance(params, tuple):
        assert len(params) == 1, "response tuple must be a singleton"

    if not encoding:
        encoding = "utf-8"

    if FastMarshaller:
        m = FastMarshaller(encoding)
    else:
        m = Marshaller(encoding, allow_none)

    data = m.dumps(params)

    if encoding != "utf-8":
        xmlheader = "<?xml version='1.0' encoding='%s'?>\n" % str(encoding)
    else:
        xmlheader = "<?xml version='1.0'?>\n"  # utf-8 is default

    # standard XML-RPC wrappings
    if methodname:
        # a method call
        if not isinstance(methodname, str):
            methodname = methodname.encode(encoding)
        data = (
            xmlheader,
            "<methodCall>\n"
            "<methodName>", methodname, "</methodName>\n",
            data,
            "</methodCall>\n"
            )
    elif methodresponse:
        # a method response, or a fault structure
        data = (
            xmlheader,
            "<methodResponse>\n",
            data,
            "</methodResponse>\n"
            )
    else:
        return data  # return as is
    return "".join(data)

##
# Convert an XML-RPC packet to a Python object.  If the XML-RPC packet
# represents a fault condition, this function raises a Fault exception.
#
# @param data An XML-RPC packet, given as an 8-bit string.
# @return A tuple containing the unpacked data, and the method name
#     (None if not present).
# @see Fault


def loads(data, use_datetime=False):
    """data -> unmarshalled data, method name

    Convert an XML-RPC packet to unmarshalled data plus a method
    name (None if not present).

    If the XML-RPC packet represents a fault condition, this function
    raises a Fault exception.
    """
    p, u = getparser(use_datetime=use_datetime)
    p.feed(data)
    p.close()
    return u.close(), u.getmethodname()

##
# Encode a string using the gzip content encoding such as specified by the
# Content-Encoding: gzip
# in the HTTP header, as described in RFC 1952
#
# @param data the unencoded data
# @return the encoded data


def gzip_encode(data):
    """data -> gzip encoded data

    Encode data using the gzip content encoding as described in RFC 1952
    """
    if not gzip:
        raise NotImplementedError
    f = BytesIO()
    gzf = gzip.GzipFile(mode="wb", fileobj=f, compresslevel=1)
    gzf.write(data)
    gzf.close()
    encoded = f.getvalue()
    f.close()
    return encoded

##
# Decode a string using the gzip content encoding such as specified by the
# Content-Encoding: gzip
# in the HTTP header, as described in RFC 1952
#
# @param data The encoded data
# @return the unencoded data
# @raises ValueError if data is not correctly coded.


def gzip_decode(data):
    """gzip encoded data -> unencoded data

    Decode data using the gzip content encoding as described in RFC 1952
    """
    if not gzip:
        raise NotImplementedError
    f = BytesIO(data)
    gzf = gzip.GzipFile(mode="rb", fileobj=f)
    try:
        decoded = gzf.read()
    except IOError:
        raise ValueError("invalid data")
    f.close()
    gzf.close()
    return decoded

##
# Return a decoded file-like object for the gzip encoding
# as described in RFC 1952.
#
# @param response A stream supporting a read() method
# @return a file-like object that the decoded data can be read() from


class GzipDecodedResponse(gzip.GzipFile if gzip else object):
    """a file-like object to decode a response encoded with the gzip
    method, as described in RFC 1952.
    """
    def __init__(self, response):
        #response doesn't support tell() and read(), required by
        #GzipFile
        if not gzip:
            raise NotImplementedError
        self.io = BytesIO(response.read())
        gzip.GzipFile.__init__(self, mode="rb", fileobj=self.io)

    def close(self):
        gzip.GzipFile.close(self)
        self.io.close()


# --------------------------------------------------------------------
# request dispatcher

class _Method:
    # some magic to bind an XML-RPC method to an RPC server.
    # supports "nested" methods (e.g. examples.getStateName)
    def __init__(self, send, name):
        self.__send = send
        self.__name = name

    def __getattr__(self, name):
        return _Method(self.__send, "%s.%s" % (self.__name, name))

    def __call__(self, *args):
        return self.__send(self.__name, args)

##
# Standard transport class for XML-RPC over HTTP.
# <p>
# You can create custom transports by subclassing this method, and
# overriding selected methods.


class Transport:
    """Handles an HTTP transaction to an XML-RPC server."""

    # client identifier (may be overridden)
    user_agent = "xmlrpclib.py/%s (by www.pythonware.com)" % __version__

    #if true, we'll request gzip encoding
    accept_gzip_encoding = True

    # if positive, encode request using gzip if it exceeds this threshold
    # note that many server will get confused, so only use it if you know
    # that they can decode such a request
    encode_threshold = None  # None = don't encode

    def __init__(self, use_datetime=False):
        self._use_datetime = use_datetime
        self._connection = (None, None)
        self._extra_headers = []

    ##
    # Send a complete request, and parse the response.
    # Retry request if a cached connection has disconnected.
    #
    # @param host Target host.
    # @param handler Target PRC handler.
    # @param request_body XML-RPC request body.
    # @param verbose Debugging flag.
    # @return Parsed response.

    def request(self, host, handler, request_body, verbose=False):
        #retry request once if cached connection has gone cold
        for i in (0, 1):
            try:
                return self.single_request(host, handler, request_body, verbose)
            except socket.error as e:
                if i or e.errno not in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE):
                    raise
            except httplib.BadStatusLine:  # close after we sent request
                if i:
                    raise

    def single_request(self, host, handler, request_body, verbose=False):
        # issue XML-RPC request
        try:
            http_conn = self.send_request(host, handler, request_body, verbose)
            resp = http_conn.getresponse()
            if resp.status == 200:
                self.verbose = verbose
                return self.parse_response(resp)

        except Fault:
            raise
        except Exception:
            #All unexpected errors leave connection in
            # a strange state, so we clear it.
            self.close()
            raise

        #We got an error response.
        #Discard any response data and raise exception
        if resp.getheader("content-length", ""):
            resp.read()
        raise ProtocolError(
            host + handler,
            resp.status, resp.reason,
            dict(resp.getheaders())
            )

    ##
    # Create parser.
    #
    # @return A 2-tuple containing a parser and a unmarshaller.

    def getparser(self):
        # get parser and unmarshaller
        return getparser(use_datetime=self._use_datetime)

    ##
    # Get authorization info from host parameter
    # Host may be a string, or a (host, x509-dict) tuple; if a string,
    # it is checked for a "user:pw@host" format, and a "Basic
    # Authentication" header is added if appropriate.
    #
    # @param host Host descriptor (URL or (URL, x509 info) tuple).
    # @return A 3-tuple containing (actual host, extra headers,
    #     x509 info).  The header and x509 fields may be None.

    def get_host_info(self, host):

        x509 = {}
        if isinstance(host, tuple):
            host, x509 = host

        auth, host = urllib_parse.splituser(host)

        if auth:
            auth = urllib_parse.unquote_to_bytes(auth)
            auth = base64.encodebytes(auth).decode("utf-8")
            auth = "".join(auth.split())  # get rid of whitespace
            extra_headers = [
                ("Authorization", "Basic " + auth)
                ]
        else:
            extra_headers = []

        return host, extra_headers, x509

    ##
    # Connect to server.
    #
    # @param host Target host.
    # @return An HTTPConnection object

    def make_connection(self, host):
        #return an existing connection if possible.  This allows
        #HTTP/1.1 keep-alive.
        if self._connection and host == self._connection[0]:
            return self._connection[1]
        # create a HTTP connection object from a host descriptor
        chost, self._extra_headers, x509 = self.get_host_info(host)
        self._connection = host, httplib.HTTPConnection(chost)
        return self._connection[1]

    ##
    # Clear any cached connection object.
    # Used in the event of socket errors.
    #
    def close(self):
        if self._connection[1]:
            self._connection[1].close()
            self._connection = (None, None)

    ##
    # Send HTTP request.
    #
    # @param host Host descriptor (URL or (URL, x509 info) tuple).
    # @param handler Targer RPC handler (a path relative to host)
    # @param request_body The XML-RPC request body
    # @param debug Enable debugging if debug is true.
    # @return An HTTPConnection.

    def send_request(self, host, handler, request_body, debug):
        connection = self.make_connection(host)
        headers = self._extra_headers[:]
        if debug:
            connection.set_debuglevel(1)
        if self.accept_gzip_encoding and gzip:
            connection.putrequest("POST", handler, skip_accept_encoding=True)
            headers.append(("Accept-Encoding", "gzip"))
        else:
            connection.putrequest("POST", handler)
        headers.append(("Content-Type", "text/xml"))
        headers.append(("User-Agent", self.user_agent))
        self.send_headers(connection, headers)
        self.send_content(connection, request_body)
        return connection

    ##
    # Send request headers.
    # This function provides a useful hook for subclassing
    #
    # @param connection httpConnection.
    # @param headers list of key,value pairs for HTTP headers

    def send_headers(self, connection, headers):
        for key, val in headers:
            connection.putheader(key, val)

    ##
    # Send request body.
    # This function provides a useful hook for subclassing
    #
    # @param connection httpConnection.
    # @param request_body XML-RPC request body.

    def send_content(self, connection, request_body):
        #optionally encode the request
        if (self.encode_threshold is not None and
            self.encode_threshold < len(request_body) and
            gzip):
            connection.putheader("Content-Encoding", "gzip")
            request_body = gzip_encode(request_body)

        connection.putheader("Content-Length", str(len(request_body)))
        connection.endheaders()

        if request_body:
            connection.send(request_body)

    ##
    # Parse response.
    #
    # @param file Stream.
    # @return Response tuple and target method.

    def parse_response(self, response):
        # read response data from httpresponse, and parse it
        # Check for new http response object, otherwise it is a file object.
        if hasattr(response, 'getheader'):
            if response.getheader("Content-Encoding", "") == "gzip":
                stream = GzipDecodedResponse(response)
            else:
                stream = response
        else:
            stream = response

        p, u = self.getparser()

        while 1:
            data = stream.read(1024)
            if not data:
                break
            if self.verbose:
                print("body:", repr(data))
            p.feed(data)

        if stream is not response:
            stream.close()
        p.close()

        return u.close()

##
# Standard transport class for XML-RPC over HTTPS.


class SafeTransport(Transport):
    """Handles an HTTPS transaction to an XML-RPC server."""

    # FIXME: mostly untested

    def make_connection(self, host):
        if self._connection and host == self._connection[0]:
            return self._connection[1]

        if not hasattr(httplib, "HTTPSConnection"):
            raise NotImplementedError(
            "your version of httplib doesn't support HTTPS")
        # create a HTTPS connection object from a host descriptor
        # host may be a string, or a (host, x509-dict) tuple
        chost, self._extra_headers, x509 = self.get_host_info(host)
        self._connection = host, httplib.HTTPSConnection(chost,
            None, **(x509 or {}))
        return self._connection[1]

##
# Standard server proxy.  This class establishes a virtual connection
# to an XML-RPC server.
# <p>
# This class is available as ServerProxy and Server.  New code should
# use ServerProxy, to avoid confusion.
#
# @def ServerProxy(uri, **options)
# @param uri The connection point on the server.
# @keyparam transport A transport factory, compatible with the
#    standard transport class.
# @keyparam encoding The default encoding used for 8-bit strings
#    (default is UTF-8).
# @keyparam verbose Use a true value to enable debugging output.
#    (printed to standard output).
# @see Transport


class Client(UnicodeMixin, object):
    """
    uri [,options] -> a logical connection to an XML-RPC server

    uri is the connection point on the server, given as
    scheme://host/target.

    The standard implementation always supports the "http" scheme.  If
    SSL socket support is available (Python 2.0), it also supports
    "https".

    If the target part and the slash preceding it are both omitted,
    "/RPC2" is assumed.

    The following options can be given as keyword arguments:

        transport: a transport factory
        encoding: the request encoding (default is UTF-8)

    All 8-bit strings passed to the server proxy are assumed to use
    the given encoding.
    """

    _supported_schemes = set(["http", "https"])

    def __init__(self, uri, transport=None, encoding=None, verbose=False, allow_none=False, *args, **kwargs):
        super(Client, self).__init__(*args, **kwargs)

        parsed = urllib_parse.urlparse(uri)

        if not parsed.scheme in self._supported_schemes:
            msg = "%s is not a support scheme. Must be one of (%s)"
            msg = msg % (parsed.scheme, ",".join(self._supported_schemes))

            raise UnsupportedScheme(msg)

        self._host = parsed.netloc
        self._handler = parsed.path if parsed.path else "/RPC2"

        self._encoding = encoding if encoding is not None else "utf-8"
        self._verbose = verbose
        self._allow_none = allow_none

        if transport is None:
            if type == "https":
                transport = SafeTransport(use_datetime=True)
            else:
                transport = Transport(use_datetime=True)

        self._transport = transport

    def __getattr__(self, name):
        if not name.startswith("_"):
            return _Method(self._request, name)

        return super(Client, self).__getattr_(name)

    def __unicode__(self):
        return "Client for %s%s" % (self._host, self._handler)

    def _close(self):
        self._transport.close()

    def _request(self, methodname, params):
        # call a method on the remote server

        request = dumps(params, methodname, encoding=self._encoding,
                        allow_none=self._allow_none).encode(self._encoding)

        response = self._transport.request(
            self._host,
            self._handler,
            request,
            verbose=self._verbose
            )

        if len(response) == 1:
            response = response[0]

        return response
