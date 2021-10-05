"""Module for encoding and decoding length delimited fields"""
import binascii
import copy
import sys
import six
import logging

from google.protobuf.internal import wire_format, encoder, decoder

import blackboxprotobuf.lib.types
from blackboxprotobuf.lib.types import varint
from blackboxprotobuf.lib.exceptions import (
    EncoderException, DecoderException, TypedefException, BlackboxProtobufException)

# Turn on to get debug messages for binary field guessing
#logging.basicConfig(level=logging.DEBUG)

# Number of messages "deep" to keep guessing. Want to cap to prevent edge cases
# where it attempts to parse a non-message binary as a message and it blows up
# the python stack
# TODO implement this for new decoder
max_guess_recursion = 5


def encode_string(value):
    try:
        value = six.ensure_text(value)
    except TypeError as exc:
        six.raise_from(EncoderException("Error encoding string to message: %r" % value), exc)
    return encode_bytes(value)

def encode_bytes(value):
    """Encode varint length followed by the string.
       This should also work to encode incoming string values.
    """
    if isinstance(value, bytearray):
        value = bytes(value)
    try:
        value = six.ensure_binary(value)
    except TypeError as exc:
        six.raise_from(EncoderException("Error encoding bytes to message: %r" % value), exc)
    encoded_length = varint.encode_varint(len(value))
    return encoded_length + value

def decode_bytes(value, pos):
    """Decode varint for the length and then returns that number of bytes"""
    length, pos = varint.decode_varint(value, pos)
    end = pos+length
    try:
        return value[pos:end], end
    except IndexError as exc:
        six.raise_from(DecoderException(
            ("Error decoding bytes. Decoded length %d is longer than bytes"
             " available %d") % (length, len(value)-pos)), exc)

def encode_bytes_hex(value):
    """Encode varint length followed by the string.
       Expects a string of hex characters
    """
    try:
        return encode_bytes(binascii.unhexlify(value))
    except (TypeError, binascii.Error) as exc:
        six.raise_from(EncoderException("Error encoding hex bytestring %s" % value), exc)

def decode_bytes_hex(buf, pos):
    """Decode varint for length and then returns that number of bytes.
       Outputs the bytes as a hex value
    """
    value, pos = decode_bytes(buf, pos)
    return binascii.hexlify(value), pos

def decode_string(value, pos):
    """Decode varint for length and then the bytes"""
    length, pos = varint.decode_varint(value, pos)
    end = pos+length
    try:
        # backslash escaping isn't reversible easily
        return value[pos:end].decode('utf-8'), end
    except (TypeError, UnicodeDecodeError) as exc:
        six.raise_from(DecoderException("Error decoding UTF-8 string %s" % value[pos:end]), exc)


def encode_message(data, typedef, path=None):
    """Encode a Python dictionary representing a protobuf message
       data - Python dictionary mapping field numbers to values
       typedef - Type information including field number, field name and field type
       This will throw an exception if an unkown value is used as a key
    """
    output = bytearray()
    if path is None:
        path = []

    # TODO Implement path for encoding
    for field_number, value in data.items():
        # Get the field number convert it as necessary
        alt_field_number = None

        if six.PY2:
            string_types = (str, unicode)
        else:
            string_types = str

        if isinstance(field_number, string_types):
            if '-' in field_number:
                field_number, alt_field_number = field_number.split('-')
            # TODO can refactor to cache the name to number mapping
            for number, info in typedef.items():
                if 'name' in info and info['name'] == field_number and field_number != '':
                    field_number = number
                    break
        else:
            field_number = str(field_number)

        field_path = path[:]
        field_path.append(field_number)

        if field_number not in typedef:
            raise EncoderException("Provided field name/number %s is not valid"
                                   % (field_number), field_path)

        field_typedef = typedef[field_number]

        # Get encoder
        if 'type' not in field_typedef:
            raise TypedefException('Field %s does not have a defined type.' % field_number, field_path)

        field_type = field_typedef['type']

        field_encoder = None
        if field_type == 'message':
            innertypedef = None
            # Check for a defined message type
            # TODO handle non dict alt_typdefs (eg. 'string'/'bytes' instead of a dict)
            if alt_field_number is not None:
                if alt_field_number not in field_typedef['alt_typedefs']:
                    raise EncoderException(
                        'Provided alt field name/number %s is not valid for field_number %s'
                        % (alt_field_number, field_number), field_path)
                innertypedef = field_typedef['alt_typedefs'][alt_field_number]
            elif 'message_typedef' in field_typedef:
                innertypedef = field_typedef['message_typedef']
            elif 'message_type_name' in field_typedef:
                message_type_name = field_typedef['message_type_name']
                if message_type_name not in blackboxprotobuf.known_messages:
                    raise TypedefException("Message type (%s) has not been defined"
                                           % field_typedef['message_type_name'], field_path)
                innertypedef = blackboxprotobuf.known_messages[message_type_name]
            else:
                raise TypedefException("Could not find message typedef for %s" % field_number, field_path)

            field_encoder = lambda data: encode_lendelim_message(data, innertypedef, path=field_path)
        else:
            if field_type not in blackboxprotobuf.lib.types.encoders:
                raise TypedefException('Unknown type: %s' % field_type)
            field_encoder = blackboxprotobuf.lib.types.encoders[field_type]
            if field_encoder is None:
                raise TypedefException('Encoder not implemented for %s' % field_type, field_path)


        # Encode the tag
        tag = encoder.TagBytes(int(field_number), blackboxprotobuf.lib.types.wiretypes[field_type])

        try:
            # Handle repeated values
            if isinstance(value, list) and not field_type.startswith('packed_'):
                for repeated in  value:
                    output += tag
                    output += field_encoder(repeated)
            else:
                output += tag
                output += field_encoder(value)
        except EncoderException as exc:
            exc.set_path(field_path)
            six.reraise(*sys.exc_info())

    return output

def decode_message(buf, typedef=None, pos=0, end=None, depth=0, path=None):
    """Decode a protobuf message with no length delimiter"""
    #TODO recalculate and re-add path for errors
    if end is None:
        end = len(buf)

    logging.debug("End: %d", end)
    if typedef is None:
        typedef = {}
    else:
        # Don't want to accidentally modify the original
        typedef = copy.deepcopy(typedef)

    if path is None:
        path = []

    output = {}

    grouped_fields, pos = _group_by_number(buf, pos, end, path)
    for (field_number, (wire_type, buffers)) in grouped_fields.items():
        # wire_type should already be validated by _group_by_number

        field_outputs = None
        field_typedef = typedef.get(field_number, {})
        field_key = _get_field_key(field_number, typedef)
        # Easy cases. Fixed size or bytes/string
        if (wire_type in [wire_format.WIRETYPE_FIXED32,
                         wire_format.WIRETYPE_FIXED64, wire_format.WIRETYPE_VARINT]
           or ('type' in field_typedef and field_typedef['type'] != 'message')):

            if 'type' not in field_typedef:
                field_typedef['type'] = blackboxprotobuf.lib.types.wire_type_defaults[wire_type]

            # we already have a type, just map the decoder
            if field_typedef['type'] not in blackboxprotobuf.lib.types.decoders:
                raise TypedefException("Got unkown type % for field_number %"
                        % (field_typedef['type'], field_number))

            decoder = blackboxprotobuf.lib.types.decoders[field_typedef['type']]
            field_outputs = [ decoder(buf, 0) for buf in buffers ]

            # We already divided up everything by length, this should be redundant
            for buf, _pos in zip(buffers, [ y for _, y in field_outputs]):
                assert(len(buf) == _pos)

            field_outputs = [ value for (value, _) in field_outputs ]
            if len(field_outputs) == 1:
                output[field_key] = field_outputs[0]
            else:
                output[field_key] = field_outputs

        elif wire_type == wire_format.WIRETYPE_LENGTH_DELIMITED:
            _try_decode_lendelim_fields(buffers, field_key, field_typedef, output)

        # Save the field typedef/type back to the typedef
        typedef[field_number] = field_typedef

    return output, typedef, pos

def _group_by_number(buf, pos, end, path):
    """ Parse through the whole message and return buffers based on wire type.
        This forces us to parse the whole message at once, but I think we're
        doing that anyway.
        Returns a dictionary like:
            {
                "2": (<wiretype>, [<data>])
            }
    """
    # TODO Add path to error messages in here
    output_map = {}
    while pos < end:
        # Read in a field
        try:
            if six.PY2:
                tag, pos = decoder._DecodeVarint(str(buf), pos)
            else:
                tag, pos = decoder._DecodeVarint(buf, pos)
        except (IndexError, decoder._DecodeError) as exc:
            six.raise_from(DecoderException(
                "Error decoding length from buffer: %r..." %
                (binascii.hexlify(buf[pos : pos+8]))), exc)

        field_number, wire_type = wire_format.UnpackTag(tag)

        # We want field numbers as strings everywhere
        field_number = str(field_number)

        if field_number in output_map and output_map[field_number][0] != wire_type:
            """ This should never happen """
            raise DecoderException("Field %s has mistmatched wiretypes. Previous: %s Now: %s"
                        % (field_number, output_map[field_number][0], wire_type))

        length = None
        if wire_type == wire_format.WIRETYPE_VARINT:
            # We actually have to read in the whole varint to figure out it's size
            _, new_pos = varint.decode_varint(buf, pos)
            length = new_pos - pos
        elif wire_type == wire_format.WIRETYPE_FIXED32:
            length = 4
        elif wire_type == wire_format.WIRETYPE_FIXED64:
            length = 8
        elif wire_type == wire_format.WIRETYPE_LENGTH_DELIMITED:
            # Read the length from the start of the message
            # add on the length of the length tag as well
            bytes_length, new_pos = varint.decode_varint(buf, pos)
            length = bytes_length + (new_pos - pos)
        elif wire_type in [wire_format.WIRETYPE_START_GROUP, wire_format.WIRETYPE_END_GROUP]:
            raise DecoderException("GROUP wire types not supported")
        else:
            raise DecoderException("Got unkown wire type: %d" % wire_type)
        if pos + length > end:
            raise DecoderException("Decoded length for field %s goes over end: %d > %d"
                    % (field_number, pos + length, end))

        field_buf = buf[pos: pos + length]

        if field_number in output_map:
            output_map[field_number][1].append(field_buf)
        else:
            output_map[field_number] = (wire_type, [field_buf])
        pos += length
    return output_map, pos

def _get_field_key(field_number, typedef):
    """If field_number has a name, then use that"""
    if not isinstance(field_number, (int, str)):
        # Should only get unpredictable inputs from encoding
        raise EncoderException("Field key in message must be a str or int")
    if isinstance(field_number, int):
        field_number = str(field_number)
    alt_field_number = None
    if '-' in field_number:
        field_number, alt_field_number = field_number.split('-')
        # TODO
        raise NotImplemented("Handling for _get_field_key not implemented for alt typedefs: %" % field_number)
    if field_number in typedef and 'name' in typedef[field_number]:
        field_key = typedef[field_number]['name']
    else:
        field_key = field_number
    # Return the new field_name + alt_field_number
    return field_key + ('' if alt_field_number is None else '-' + alt_field_number)

def _try_decode_lendelim_fields(buffers, field_key, field_typedef, message_output):
    # This is where things get weird
    # To start, since we want to decode messages and not treat every
    # embedded message as bytes, we have to guess if it's a message or
    # not.
    # Unlike other types, we can't assume our message types are
    # consistent across the tree or even within the same message.
    # A type could be a bytes type that might decode to different
    # messages that don't have the same type definition. This is where
    # 'alt_typedefs' let us say, these are the different message types we've
    # seen for this one field.
    # In general, if something decodes as a message once, the rest should too
    # and we can enforce that across a single message, but not multiple
    # messages.
    # This is going to change the definition of "alt_typedefs" a bit from just
    # alternate message type definitions to also allowing downgrading to
    # 'bytes' or string with an 'alt_type' if it doesn't parse

    # can configure defaults like 'bytes_hex'
    default_bytes_type = blackboxprotobuf.lib.types.default_binary_type

    try:
        outputs_map = {}
        # grab all dictonary alt_typedefs
        all_typedefs = { key: value for key, value in
                            field_typedef.get('alt_typedefs', {}).items() if isinstance(value, dict) }
        all_typedefs["1"] = field_typedef.get('message_typedef', {})

        out_typedefs = {}


        for buf in buffers:
            output = None
            output_typedef = None
            output_typedef_num = None
            for alt_typedef_num, alt_typedef in sorted(all_typedefs.items(), key=lambda x: int(x[0])):
                try:
                    output, output_typedef, _ = decode_lendelim_message(buf, alt_typedef)
                except:
                    continue
                output_typedef_num = alt_typedef_num
                break
            # try an anonymous type
            # let the error propogate up if we fail this
            if output is None:
                output, output_typedef, _ = decode_lendelim_message(buf, {})
                output_typedef_num = str( max([ int(i) for i in ["0"] + all_typedefs.keys()]) + 1 )

            # save the output or typedef we found
            out_typedefs[output_typedef_num] = output_typedef
            output_list = outputs_map.get(output_typedef_num, [])
            output_list.append(output)
            outputs_map[output_typedef_num] = output_list
        # was able to decode everything as a message
        field_typedef['type'] = 'message'
        field_typedef['message_typedef'] = all_typedefs["1"]
        if len(all_typedefs.keys()) > 1:
            del all_typedefs["1"]
            field_typedef['alt_typedefs'].update(all_typedefs)
        # messages get set as "key-alt_number"
        for output_typedef_num, outputs in outputs_map.items():
            output_field_key = field_key
            if output_typedef_num != "1":
                output_field_key += "-" + output_typedef_num
            message_output[output_field_key] = outputs if len(outputs) > 1 else outputs[0]
        # success, return
        return
    except DecoderException as exc:
        # this should be pretty common, don't be noisy or throw an exception
        logging.debug("Could not decode a buffer for field number % as a message: ", field_key, exc)

    # Decoding as a message did not work, try strings and then bytes
    # The bytes decoding should never fail
    for target_type in ['string', default_bytes_type]:
        try:
            outputs = []
            decoder = blackboxprotobuf.lib.types.decoders[target_type]
            for buf in buffers:
                output, _ = decoder(buf, 0)
                outputs.append(output)

            # all outputs worked, this is our type
            # check if there is a message type already in the typedef
            if 'type' in field_typedef and 'message' == field_typedef['type']:
                # we already had a message type. save it as an alt_typedef

                # check if we already have this type as an alt_typedef
                output_typedef_nums = { key: value for key, value in field_key['alt_typedefs'].items() if value == target_type }.keys()
                output_typedef_num = None
                if len(output_typedef_nums) == 0:
                    output_typedef_num = str( max([ int(i) for i in ["0"] + all_typedefs.keys()]) + 1 )
                    field_typedef['alt_typedefs'][output_typedef_num] = target_typedef
                else:
                    output_typedef_num = output_typedef_nums[0]
                message_output[field_key + "-" + output_tyepdef_num] = outputs if len(outputs) > 1 else outputs[0]
            else:
                field_typedef['type'] = target_type
                message_output[field_key] = outputs if len(outputs) > 1 else outputs[0]
            return
        except DecoderException as exc:
            continue

def encode_lendelim_message(data, typedef, path=None):
    """Encode the length before the message"""
    message_out = encode_message(data, typedef, path=path)
    length = varint.encode_varint(len(message_out))
    logging.debug("Message length encoded: %d", len(length) + len(message_out))
    return length + message_out

def decode_lendelim_message(buf, typedef=None, pos=0, depth=0, path=None):
    """Read in the length and use it as the end"""
    length, pos = varint.decode_varint(buf, pos)
    ret = decode_message(buf, typedef, pos, pos+length, depth=depth, path=path)
    return ret

def generate_packed_encoder(wrapped_encoder):
    """Generate an encoder for a packed type from the base type encoder"""
    def length_wrapper(values):
        """Encode repeat values and prefix with the length"""
        output = bytearray()
        for value in values:
            output += wrapped_encoder(value)
        length = varint.encode_varint(len(output))
        return length + output
    return length_wrapper

def generate_packed_decoder(wrapped_decoder):
    """Generate an decoder for a packer type from a base type decoder"""
    def length_wrapper(buf, pos):
        """Decode repeat values prefixed with the length"""
        length, pos = varint.decode_varint(buf, pos)
        end = pos + length
        output = []
        while pos < end:
            value, pos = wrapped_decoder(buf, pos)
            output.append(value)
        if pos > end:
            raise DecoderException(
                ("Error decoding packed field. Packed length larger than"
                 " buffer: decoded = %d, left = %d")
                % (length, len(buf) - pos))
        return output, pos
    return length_wrapper
