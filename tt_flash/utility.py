import argparse
import subprocess
import re
import sys
import inspect
import os
import yaml
from pathlib import Path
import datetime
import importlib
from importlib.resources import path

from tt_flash.error import TTError

# Error code indicating we should not continue running anything on this board as it is hung
BOARD_HUNG_EXIT_CODE = 99
# Indicates that the process never finished
DID_NOT_FINISH_EXIT_CODE = 97
# Reset failure
FAILED_RESET_BOARD_EXIT_CODE = 96
#Thermal trip
THERMAL_RUNAWAY_EXIT_CODE = 95

def get_env_var(var_name, default=None):
    return os.getenv(var_name)


def get_error_code_for_error_msg(error_msg):
    if "0xffffffff from ARC scratch[2]" in error_msg:
        return BOARD_HUNG_EXIT_CODE
    elif "THERMAL RUNAWAY or Production FW hung" in error_msg:
        return THERMAL_RUNAWAY_EXIT_CODE
    return 11


# Returns the root path of the package, so we can access data files and such
def package_root_path():
    return path("tt_flash", "")

# Get path of this script. 'frozen' means packaged with pyinstaller.
def application_path ():
    if getattr(sys, 'frozen', False):
        application_path = os.path.dirname(sys.executable)
    elif __file__:
        application_path = os.path.dirname(__file__)
    return application_path

class colors:
    CEND = "\33[0m"
    CBOLD = "\33[1m"
    CITALIC = "\33[3m"
    CURL = "\33[4m"
    CBLINK = "\33[5m"
    CBLINK2 = "\33[6m"
    CSELECTED = "\33[7m"
    CBLACK = "\33[30m"
    CRED = "\33[31m"
    CGREEN = "\33[32m"
    CYELLOW = "\33[33m"
    CBLUE = "\33[34m"
    CVIOLET = "\33[35m"
    CBEIGE = "\33[36m"
    CWHITE = "\33[37m"
    CBLACKBG = "\33[40m"
    CREDBG = "\33[41m"
    CGREENBG = "\33[42m"
    CYELLOWBG = "\33[43m"
    CBLUEBG = "\33[44m"
    CVIOLETBG = "\33[45m"
    CBEIGEBG = "\33[46m"
    CWHITEBG = "\33[47m"


def add_arguments(parser):
    parser.add_argument(
        "--no-color", action="store_true", default=False, help="Do not colorize output"
    )
    parser.add_argument(
        "--verbose", action="store_true", default=False, help="Print all the detail"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Do not execute; only print",
    )


def setup(args):
    global g_args
    g_args = args


def VERBOSE(a):
    if g_args and g_args.verbose:
        print(f"  {a}")


def INFO(a):
    print(a)


def PRINT(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print(f"{colors.CBLUE}{a}{colors.CEND}", *args, **kwargs)


def PRINT_RED(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print(f"{colors.CRED}{a}{colors.CEND}", *args, **kwargs)


def PRINT_GREEN(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print(f"{colors.CGREEN}{a}{colors.CEND}", *args, **kwargs)


def PRINT_YELLOW(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print(f"{colors.CYELLOW}{a}{colors.CEND}", *args, **kwargs)

def PRINT_VIOLET(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print(f"{colors.CVIOLET}{a}{colors.CEND}", *args, **kwargs)

def PRINT_GREENBG(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print (f"{colors.CGREENBG}{colors.CBLACK}{a}{colors.CEND}", *args, **kwargs)

def PRINT_YELLOWBG(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print (f"{colors.CYELLOWBG}{colors.CBLACK}{a}{colors.CEND}", *args, **kwargs)

def PRINT_REDBG(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print (f"{colors.CREDBG}{colors.CBLACK}{a}{colors.CEND}", *args, **kwargs)

def PRINT_BLUEBG(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print (f"{colors.CBLUEBG}{colors.CBLACK}{a}{colors.CEND}", *args, **kwargs)

def PRINT_VIOLETBG(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print (f"{colors.CVIOLETBG}{colors.CBLACK}{a}{colors.CEND}", *args, **kwargs)

def ERROR(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print(f"{colors.CRED}ERROR: {a}{colors.CEND}", *args, **kwargs)

def FAIL(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print(f"{colors.CRED}FAIL:{colors.CEND} {a}", *args, **kwargs)

def SUCCESS(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print(f"{colors.CGREEN}SUCCESS:{colors.CEND} {a}", *args, **kwargs)

def FATAL(a, *args, **kwargs):
    ERROR(a, *args, **kwargs)
    sys.exit(1)


def WARN(a, *args, **kwargs):
    if g_args and g_args.no_color:
        print(a, *args, **kwargs)
    else:
        print(f"{colors.CYELLOW}WARNING: {a}{colors.CEND}", *args, **kwargs)


def log_all(my_file):
    return [lambda l: my_file.write(l + "\n")]


def print_all():
    return [lambda l: INFO(l)]


def print_on_regex(rex):
    def maprint(l, rex):
        if rex.match(l):
            INFO(l)

    return [lambda l: maprint(l, rex)]


def divider():
    return "-------------------------------------------------------------------------------------------------------------------------"


# Parses a line with regex 'rex' and puts the first capture group in 'target_dict' under given key
def read_float(l, rex, target_dict, target_dict_key):
    ma = rex.match(l)
    if ma:
        target_dict[target_dict_key] = float(ma.group(1))


def assert_setpoint(setpoint, key, expected_value, error_threshold):
    if abs(setpoint[key] - expected_value) >= error_threshold:
        ERROR(
            "Value for '%s' not set properly (expected=%.10f, reading=%0.10f)"
            % (key, expected_value, setpoint[key])
        )
        sys.exit(51)


# Runs a command 'run_array' with environment 'env'. For each line of stdout applies
# all functions given in 'parser_array' to allow one to extract the output.
def run_cmd(run_array, env={}, parser_array=None, dir="", host=None, dry_run=False):
    # 1. Create export string for the env vars supplied in 'env'
    export_env_as_str = ""
    if len(env) > 0:
        export_env_array = []
        for e in env:
            export_env_array = ["%s='%s'" % (e, env[e])] + export_env_array
        export_env_as_str = "export %s;" % (" ".join(export_env_array))
        VERBOSE("Exporting environment: %s" % export_env_as_str)

    # 2. Create a cd string to change dir
    cd_as_str = ""
    if dir and len(dir) > 0:
        cd_as_str = cd_as_str + "cd " + dir + ";"
        VERBOSE("cd command: %s" % cd_as_str)

    run_as_str = export_env_as_str + cd_as_str + " ".join(run_array) + ";"

    # 3. Create local or ssh command
    if host:
        VERBOSE("Running on host: %s" % host)
        run_array = ["ssh", host, "bash", "-c", run_as_str]
    else:
        run_array = ["bash", "-c", run_as_str]

    # 4. Execute the command
    proc = subprocess.Popen(run_array, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for line in proc.stdout:
        if parser_array:
            for p in parser_array:
                p(line.decode("utf-8").rstrip())
    for line in proc.stderr:
        PRINT_RED("STDERR: " + line.decode("utf-8").rstrip())
    proc.communicate()[0]

    if proc.returncode != 0:
        PRINT_RED(
            "Command '%s' failed with return code %d"
            % (" ".join(run_array), proc.returncode)
        )

    return proc.returncode


def from_human(numstr):
    CONV_TABLE = {"K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}

    result = re.match("([0-9.]+)(.)?", numstr)
    if result:
        num = int(result.group(1))
        mult = result.group(2)
        if mult is None:
            mult = 1
        else:
            if mult not in CONV_TABLE:
                FATAL("I don't recognize multiplier '%s' in '%s'" % (mult, numstr))
            mult = CONV_TABLE[mult]
        return int(num) * int(mult)
    else:
        FATAL("Cannot parse number %s" % numstr)


def to_human(num):
    if num < 1024:
        return str(num)
    if num >= 1024 and num < 1024 * 1024:
        return "%.2fK" % (num / 1024)
    if num >= 1024 * 1024 and num < 1024 * 1024 * 1024:
        return "%.2fM" % (num / (1024 * 1024))
    return "%.2fG" % (num / (1024 * 1024 * 1024))


# basic_sweep takes an array of variable specifiers, and call 'callback' function
# for each combination of variables.
# The first element of the SWEEP_SPEC is the outermost loop, the bottom is the inermost loop
# For example:
# SWEEP_SPEC = [
#     { "name" : "ENV_PCI_MAX_READ_REQUEST_SIZE", "values" : range(0,4) },
#     { "name" : "ENV_PCI_MAX_PAYLOAD_SIZE",      "values" : range(0,6) }
# ]
def basic_sweep(SWEEP_SPEC, sweep_set_point={}, level=0, callback=None):
    sweep_var = SWEEP_SPEC[level]
    var_name = sweep_var["name"]
    var_values = sweep_var["values"]

    if var_name not in sweep_set_point:
        for var_value in var_values:
            sweep_set_point[var_name] = var_value
            if level == len(SWEEP_SPEC) - 1:
                if callback:
                    callback(sweep_set_point)
                else:
                    print("Would run set point: %s" % sweep_set_point)
            else:
                recursive_sweep(SWEEP_SPEC, sweep_set_point, level + 1, callback)
        del sweep_set_point[var_name]


# Formats dictionaries for pretty printing.
# Usage example:
#   print ("%s" % utility.pretty(my_dictionary))
class Formatter(object):
    def __init__(self):
        self.types = {}
        self.htchar = "    "
        self.lfchar = "\n"
        self.indent = 0
        self.set_formater(object, self.__class__.format_object)
        self.set_formater(dict, self.__class__.format_dict)
        self.set_formater(list, self.__class__.format_list)
        self.set_formater(tuple, self.__class__.format_tuple)

    def set_formater(self, obj, callback):
        self.types[obj] = callback

    def __call__(self, value, **args):
        for key in args:
            setattr(self, key, args[key])
        formater = self.types[type(value) if type(value) in self.types else object]
        return formater(self, value, self.indent)

    def format_object(self, value, indent):
        return repr(value)

    def format_dict(self, value, indent):
        lfchar = "\n"
        items = [
            lfchar
            + self.htchar * (indent + 1)
            + repr(key)
            + ": "
            + (
                self.types[
                    type(value[key]) if type(value[key]) in self.types else object
                ]
            )(self, value[key], indent + 1)
            for key in value
        ]
        return "{%s}" % (",".join(items) + lfchar + self.htchar * indent)

    def format_list(self, value, indent):
        lfchar = self.lfchar
        items = [
            lfchar
            + self.htchar * (indent + 1)
            + (self.types[type(item) if type(item) in self.types else object])(
                self, item, indent + 1
            )
            for item in value
        ]
        return "[%s]" % (",".join(items) + lfchar + self.htchar * indent)

    def format_list_singleline(self, value, indent):
        lfchar = self.lfchar
        items = [
            (self.types[type(item) if type(item) in self.types else object])(
                self, item, indent + 1
            )
            for item in value
        ]
        return "[ %s ]" % (", ".join(items))

    def format_tuple(self, value, indent):
        lfchar = self.lfchar
        items = [
            lfchar
            + self.htchar * (indent + 1)
            + (self.types[type(item) if type(item) in self.types else object])(
                self, item, indent + 1
            )
            for item in value
        ]
        return "(%s)" % (",".join(items) + lfchar + self.htchar * indent)

    def format_tuple_singleline(self, value, indent):
        lfchar = self.lfchar
        items = [
            (self.types[type(item) if type(item) in self.types else object])(
                self, item, indent + 1
            )
            for item in value
        ]
        return "( %s )" % (", ".join(items))


pretty = Formatter()
# By default, print lists on single line.
pretty.set_formater(list, pretty.__class__.format_list_singleline)
pretty.set_formater(tuple, pretty.__class__.format_tuple_singleline)

g_args = None

# Parses a string
INDEX_REGEXP = re.compile(r"\[\s*(\d*)\s*\]")
NAME_WITH_INDEX_REGEXP = re.compile(r"([^[\t ]*)\s*(\[\s*\d*\s*\])?")


def parse_indexed_register(name):
    m = re.match(NAME_WITH_INDEX_REGEXP, name)

    if m:
        index = None
        if m.group(2) is not None:
            index = int(re.match(INDEX_REGEXP, m.group(2)).group(1))
        ret_val = (m.group(1), index)
    else:
        ret_val = None
    return ret_val


def stacktrace(skip=0, full=False):
    ret_str = ""
    i = 0
    for frame in reversed(inspect.stack()):
        if (i > 0 and i < len(inspect.stack()) - 1 - skip) or full:
            argvalues = inspect.getargvalues(frame[0])
            ret_str += f"{'  '*i}{os.path.basename(frame.filename)}:{frame.lineno} {frame.function}({argvalues.locals})\n"
        i += 1
    return ret_str


def print_stacktrace(full=False):
    s = stacktrace(skip=1, full=full)
    print(s.strip())


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        add_arguments(self)

    def parse_args(self, *args, **kwargs):
        args = super().parse_args(*args, **kwargs)

        global g_args
        g_args = args

        return args

class ExtendAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest) or []
        items.extend(values)
        setattr(namespace, self.dest, items)

class FileExistsAction(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if not os.path.exists(value):
            parser.error(f"The file {value} does not exist.")
        setattr(namespace, self.dest, items)

def register_argparse_actions(parser):
    parser.register('action', 'extend', ExtendAction)
    parser.register('action', 'file_exists', FileExistsAction)

# Flips bytes in a 32 bit integer
def flip_bytes_32(a):
    return (
        ((a & 0xFF) << 24)
        | ((a & 0xFF00) << 8)
        | ((a & 0xFF0000) >> 8)
        | ((a & 0xFF000000) >> 24)
    )


# This allow one to get dictonary's elements by saying a.b instead a["b"]
class AtDict(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


# Parses args in form a=b,c=d into a dict { "a":"b", "c":"d" }
def parse_args(args):
    parsed_args = AtDict()

    if args:
        for kv_pair in args.split(","):
            k, v = kv_pair.split("=")
            parsed_args[k] = v

    return parsed_args


# Return whether 'a' is within 'max_error_pct' of 'target_value'
def in_target_pct_error(a, target_value, max_error_pct):
    abs_error = 1.0 * target_value * max_error_pct / 100.0
    return a >= target_value - abs_error and a <= target_value + abs_error


def in_target_min_max(a, min, max):
    return a >= min and a <= max


def print_pass_fail(exit_code, comment=""):
    if comment:
        comment = " %s" % comment  # Prepend space

    if exit_code == 0:
        PRINT_GREEN("<PASSED>%s" % comment)
    else:
        PRINT_RED("<FAILED>%s" % comment)

# Remove extension from filename
def remove_extension(filename):
    return os.path.splitext(filename)[0]


def get_extension(filename):
    file_base, file_extension = os.path.splitext(filename)
    return file_extension


# Return universal timestamp
def get_timestamp():
    return datetime.datetime.utcnow()


# Recursively removes a directory
def rmdir_recursive(directory):
    directory = Path(directory)
    if directory.is_dir():
        VERBOSE("Removing directory: '%s'" % directory)
        for item in directory.iterdir():
            if item.is_dir():
                rmdir_recursive(item)
            else:
                item.unlink()
        directory.rmdir()


# Creates a directory, by default like mkdir -p
def mkdir(directory, parents=True, exist_ok=True):
    Path(directory).mkdir(parents=parents, exist_ok=exist_ok)


# Appends before extension
def add_to_filename(filename, stuff_to_add):
    file_base, file_extension = os.path.splitext(filename)
    return f"{file_base}.{stuff_to_add}{file_extension}"


# Renames file/dir by adding last-modified timestamp
def move_file_if_exists(fname):
    renamed_file = None
    assert fname
    if os.path.isfile(fname) or os.path.isdir(fname):
        statbuf = os.stat(fname)
        mod_timestamp = datetime.datetime.fromtimestamp(statbuf.st_mtime)
        time_str = mod_timestamp.strftime("%y-%m-%d-%I:%M:%S")
        renamed_file = add_to_filename(fname, time_str)
        os.rename(fname, renamed_file)
        PRINT_YELLOW(
            "Existing summary file '%s' was preserved as '%s'" % (fname, renamed_file)
        )

    return renamed_file


# Improved yaml loader that supports !include directive
class YamlLoaderWithInclude(yaml.SafeLoader):
    def __init__(self, stream):
        self._root = os.path.split(stream.name)[0]
        super(YamlLoaderWithInclude, self).__init__(stream)

    def include(self, node):
        filename = os.path.join(self._root, self.construct_scalar(node))
        with open(filename, "r") as f:
            return yaml.load(f, YamlLoaderWithInclude)


YamlLoaderWithInclude.add_constructor("!include", YamlLoaderWithInclude.include)

# Dumps data to a yaml file
def write_to_yaml_file(data, filename, force_create_dir=True):
    if force_create_dir:
        dir_name = os.path.dirname(filename)
        mkdir(dir_name)

    with open(filename, "w+") as outfile:
        yaml.dump(data, outfile, width=160, indent=2, default_flow_style=None)


# Appends data to yaml file
def append_to_yaml_file(data, filename, create_on_missing=True):
    cur_yaml = None

    if os.path.isfile(filename):
        with open(filename, "r") as infile:
            cur_yaml = yaml.safe_load(infile)
    elif create_on_missing:
        cur_yaml = []

    if cur_yaml is None:
        raise TTError("Cannot append to yaml file '%s'" % filename)
        return
    elif type(cur_yaml) is list:
        cur_yaml.append(data)
    elif type(cur_yaml) is dict:
        raise TTError(
            "Cannot append to yaml file '%s' as it does not hold an array" % filename
        )

    with open(filename, "w+") as outfile:
        yaml.dump(cur_yaml, outfile, width=160, indent=2, default_flow_style=None)


# Read given yaml file, and return python object
def read_yaml_file(filename):
    cur_yaml = None
    with open(filename, "r") as infile:
        cur_yaml = yaml.safe_load(infile)
    return cur_yaml


# Delete file if it exists
def delete_file(filename):
    if os.path.isfile(filename):
        os.remove(filename)


# This decorator allows us to call 'hooks' before and after executing
# decorated object methods. The object must have __hooks_array variable with
# the implementation of pre_... and post... functions to be called. This
# can be done by obj.__hooks_array = importlib.import_module("my_hooks")
def hooks_decorator(func):
    def wrapper(*args, **kwargs):
        if hasattr(args[0], "__hooks_array"):
            for hook in args[0].__hooks_array:
                if hasattr(hook, "pre_" + func.__name__):
                    getattr(hook, "pre_" + func.__name__)(*args, **kwargs)

        result = func(*args, **kwargs)

        if hasattr(args[0], "__hooks_array"):
            # HACK(drosen): we want the hooks to reverse execution order when finishing
            #               ensuring this is done predictibly is hard so for now we are just
            #               going to assume that post_ttx_run_end is a cleanup function.
            if func.__name__ == "ttx_run_end":
                array = reversed(args[0].__hooks_array)
            else:
                array = args[0].__hooks_array
            for hook in array:
                if hasattr(hook, "post_" + func.__name__):
                    getattr(hook, "post_" + func.__name__)(*args, **kwargs)

        return result

    return wrapper


def register_hooks(my_object, hooks_array):
    for hook in hooks_array:
        possible_hooks_file = [
            f"{os.getcwd()}/{hook}",
            f"{package_root_path()}/scripts/hooks/{hook}",
        ]
        hooks_file = None
        for phf in possible_hooks_file:
            if os.path.isfile(phf):
                hooks_file = phf
        if not hooks_file:
            raise TTError(
                f"Could not find hooks file {hook} in either of: {possible_hooks_file}"
            )

        PRINT(f"Registering hooks file {hooks_file}")
        script_dirname = os.path.dirname(hooks_file)
        if script_dirname not in sys.path:
            sys.path.append(script_dirname)

        script_basename = os.path.splitext(os.path.basename(hooks_file))[0]
        if not hasattr(my_object, "__hooks_array"):
            my_object.__hooks_array = []
        hooks_module = importlib.import_module(script_basename)
        hooks_module.__args = []  # To pass arguments into the hooks
        hooks_module.__results = {}  # To extract results from the hooks

        my_object.__hooks_array.append(hooks_module)


# Creates a reverse-mapping list so that ret[l[x]]=x
def reverse_mapping_list(l):
    ret = [0] * len(l)
    for idx, val in enumerate(l):
        ret[val] = idx
    return ret

# Loads hex file and for each word calls store_function(byte_addr, data)
def read_hex_file (filename, store_function):
    bytes_written = 0
    with open(filename) as f:
        byte_addr = 0
        for line in f:
            a = line.split ('@')
            if len(a)==2: # Address change
                byte_addr = int (a[1], 16) * 4   # Parse hex number, hence 16
            else:         # Data
                data = int (a[0], 16)
                store_function(byte_addr, data)

                byte_addr = byte_addr + 4
                bytes_written = bytes_written + 4

def load_ttp (name):
    scriptpath = package_root_path() + '/scripts'
    remove = False
    if (scriptpath not in sys.path):
        sys.path.append(scriptpath)
        remove = True

    m = None
    try:
        m = importlib.import_module(name)
    except:
        print("Could not load ttp script [" + name + "]")
        m = None

    if (remove):
        sys.path.pop()

    return m

def linspace(start, stop, delta):
    if ((stop-start)*delta < 0 or delta == 0):
        ERROR("Given linspace bounds would result in an (almost) infinite loop")
        return

    cur = start
    while (cur < stop + delta/2):
        yield cur
        cur += delta

def semver_to_hex(semver: str):
    """Converts a semantic version string from format 10.15.1 to hex 0x0A0F0100"""
    major, minor, patch = semver.split('.')
    byte_array = bytearray([0, int(major), int(minor), int(patch)])
    return f"{int.from_bytes(byte_array, byteorder='big'):08x}"

def date_to_hex(date: int):
    """Converts a given date string from format YYYYMMDDHHMM to hex 0xYMDDHHMM"""
    year = int(date[0:4]) - 2020
    month = int(date[4:6])
    day = int(date[6:8])
    hour = int(date[8:10])
    minute = int(date[10:12])
    byte_array = bytearray([year*16+month, day, hour, minute])
    return f"{int.from_bytes(byte_array, byteorder='big'):08x}"

def hex_to_semver(hexsemver: int):
    """Converts a semantic version string from format 0x0A0F0100 to 10.15.1"""
    major = hexsemver >> 16 & 0xFF;
    minor = hexsemver >>  8 & 0xFF;
    patch = hexsemver >>  0 & 0xFF;
    return f"{major}.{minor}.{patch}"

def hex_to_date(hexdate: int):
    """Converts a date given in hex from format 0xYMDDHHMM to string YYYY-MM-DD HH:MM"""
    year = (hexdate >> 28 & 0xF) + 2020;
    month = hexdate >> 24 & 0xF;
    day = hexdate >> 16 & 0xFF;
    hour = hexdate >> 8 & 0xFF;
    minute = hexdate & 0xFF;

    return f"{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}"
