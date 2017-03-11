from ticdat.utils import verify, containerish, stringish, find_duplicates_from_dict_ticdat
import ticdat.utils as tu
from ticdat.ticdatfactory import TicDatFactory
import os, subprocess, inspect, time, uuid, shutil
from collections import defaultdict
from ticdat.jsontd import make_json_dict

INFINITY = 999999

opl_keywords = ["initial", "template", "struct", "all", "and", "assert", "boolean", "constraints", "CP",
                "CPLEX", "cumulFunction", "DBConnection", "DBExecute", "DBRead", "DBUpdate", "dexpr",
                "diff", "div", "dvar", "else", "execute", "false", "float", "float+", "forall", "from",
                "in", "if", "include", "infinity", "int", "int+", "intensity", "inter", "interval",
                "invoke", "key", "main", "max", "maximize", "maxint", "min", "minimize", "mod", "not",
                "optional", "or", "ordered", "piecewise", "prepare", "prod", "pwlFunction", "range",
                "reversed", "sequence", "setof", "SheetConnection", "SheetRead", "SheetWrite", "size",
                "sorted", "SPSSConnection", "SPSSRead", "stateFunction", "stepFunction", "stepwise",
                "string", "subject", "sum", "symdiff", "to", "true", "tuple", "types", "union", "using", "with"]

def _code_dir():
    return os.path.dirname(os.path.abspath(inspect.getsourcefile(_code_dir)))

def pattern_finder(string, pattern, rsearch=False):
    """
    Searches a string for the pattern ignoring whitespace
    :param string: A text string
    :param pattern: A string containing the pattern to search for
    :param rsearch: Optional parameter indicating if the search should be performed backwards
    """
    verify(len(pattern) <= len(string), "Pattern is larger than string, cannot be found. Pattern is '%s'" % pattern)
    poss_string = []
    nospaces = lambda k: list(filter(lambda j: not j.isspace(), k))
    if rsearch:
        pattern = pattern[::-1]
        string = string[::-1]
    for i, j in enumerate(string):
        if len(nospaces(poss_string)) < len(pattern):
            poss_string.append(str(j))
        else:
            while (poss_string[0].isspace()):
                poss_string.pop(0)
            poss_string.pop(0)
            poss_string.append(str(j))
            pass
        if ''.join(nospaces(poss_string)) == pattern:
            while (poss_string[0].isspace()):
                poss_string.pop(0)
            if rsearch:
                return len(string) - i + len(poss_string) - 2
            return i + 1 - len(poss_string)
    return False

def _find_case_space_duplicates(tdf):
    """
    Finds fields that are case space duplicates
    :param tdf: A TicDatFactory defining the schema
    :return: A dictionary with the keys being tables that have case space duplicates
    """
    schema = tdf.schema()
    tables_with_case_insensitive_dups = {}
    for table in schema:
        fields = set(schema[table][0]).union(schema[table][1])
        case_insensitive_fields = set(map(lambda k: k.lower().replace(" ", "_"),fields))
        if len(fields) != len(case_insensitive_fields):
            tables_with_case_insensitive_dups[table] = fields
    return tables_with_case_insensitive_dups

def _change_fields_with_opl_keywords(tdf, undo=False):
    tdf_schema = tdf.schema()
    mapping = {}
    for table, fields in tdf_schema.items():
        for fields_list in [fields[0], fields[1]]:
            for findex in range(len(fields_list)):
                original_field = fields_list[findex]
                if not undo:
                    verify(not fields_list[findex].startswith('_'),
                           ("Field names cannot start with '_', in table %s : " +
                            "field is %s") % (table, fields_list[findex]))
                    if fields_list[findex].lower() in opl_keywords:
                        fields_list[findex] = '_' + fields_list[findex]
                else:
                    if fields_list[findex].startswith('_'):
                        fields_list[findex] = fields_list[findex][1:]
                mapping[table,original_field] = fields_list[findex]

    rtn = TicDatFactory(**tdf_schema)
    for (table, original_field),new_field in mapping.items():
        if original_field in tdf.default_values.get(table, ()):
            rtn.set_default_value(table, new_field,
                                  tdf.default_values[table][original_field])
        if original_field in tdf.data_types.get(table, ()):
            rtn.set_data_type(table, new_field,
                              *(tdf.data_types[table][original_field]))
    rtn.opl_prepend = tdf.opl_prepend
    return rtn

def _fix_fields_with_opl_keywords(tdf):
    return _change_fields_with_opl_keywords(tdf)

def _unfix_fields_with_opl_keywords(tdf):
    return _change_fields_with_opl_keywords(tdf, True)

def opl_run(mod_file, input_tdf, input_dat, soln_tdf, infinity=INFINITY, oplrun_path=None, post_solve=None):
    """
    solve an optimization problem using an OPL .mod file
    :param mod_file: An OPL .mod file.
    :param input_tdf: A TicDatFactory defining the input schema
    :param input_dat: A TicDat object consistent with input_tdf
    :param soln_tdf: A TicDatFactory defining the solution schema
    :param infinity: A number used to represent infinity in OPL
    :return: a TicDat object consistent with soln_tdf, or None if no solution found
    """
    verify(os.path.isfile(mod_file), "mod_file %s is not a valid file."%mod_file)
    verify(not _find_case_space_duplicates(input_tdf), "There are case space duplicate field names in the input schema.")
    verify(not _find_case_space_duplicates(soln_tdf), "There are case space duplicate field names in the solution schema.")
    verify(len({input_tdf.opl_prepend + t for t in input_tdf.all_tables}.union(
               {soln_tdf.opl_prepend + t for t in soln_tdf.all_tables})) ==
           len(input_tdf.all_tables) + len(soln_tdf.all_tables),
           "There are colliding input and solution table names.\nSet opl_prepend so " +
           "as to insure the input and solution table names are effectively distinct.")
    msg  = []
    verify(input_tdf.good_tic_dat_object(input_dat, msg.append),
           "tic_dat not a good object for the input_tdf factory : %s"%"\n".join(msg))
    orig_input_tdf, orig_soln_tdf = input_tdf, soln_tdf
    input_tdf = _fix_fields_with_opl_keywords(input_tdf)
    soln_tdf = _fix_fields_with_opl_keywords(soln_tdf)
    input_dat = input_tdf.TicDat(**make_json_dict(orig_input_tdf, input_dat))
    assert input_tdf.good_tic_dat_object(input_dat)
    mod_file_name = os.path.basename(mod_file)[:-4]
    with open(mod_file, "r") as f:
        mod = f.read()
        assert 'IloOplOutputFile("results.dat")' in mod
        assert ("ticdat_" + mod_file_name + ".mod") in mod
        assert ("ticdat_" + mod_file_name + "_output.mod") in mod
    working_dir = os.path.abspath(os.path.dirname(mod_file))
    if tu.development_deployed_environment:
        working_dir = os.path.join(working_dir, "oplticdat_%s"%uuid.uuid4())
        shutil.rmtree(working_dir, ignore_errors = True)
        os.mkdir(working_dir)
        working_dir = os.path.abspath(working_dir)
        _ = os.path.join(working_dir, os.path.basename(mod_file))
        shutil.copy(mod_file, _)
        mod_file = _
    datfile = os.path.join(working_dir, "temp.dat")
    output_txt = os.path.join(working_dir, "output.txt")
    results_dat = os.path.join(working_dir, "results.dat")
    if os.path.isfile(results_dat):
        os.remove(results_dat)
    with open(datfile, "w") as f:
        f.write(create_opl_text(input_tdf, input_dat, infinity))
    verify(os.path.isfile(datfile), "Could not create temp.dat")
    with open(os.path.join(working_dir, "ticdat_"+mod_file_name+".mod"), "w") as f:
        f.write("/* Autogenerated input file, created by opl.py on " + time.asctime() + " */\n")
        f.write(create_opl_mod_text(orig_input_tdf))
    with open(os.path.join(working_dir,"ticdat_"+mod_file_name+"_output.mod"), "w") as f:
        f.write("/* Autogenerated output file, created by opl.py on " + time.asctime() + " */\n")
        f.write(create_opl_mod_output_text(orig_soln_tdf))
    if not oplrun_path:
        verify(os.path.isfile(os.path.join(_code_dir(),"oplrun_path.txt")),
               "need to either pass oplrun_path argument or run oplrun_setup.py")
        with open(os.path.join(_code_dir(),"oplrun_path.txt"),"r") as f:
            oplrun_path = f.read()
    verify(os.path.isfile(oplrun_path), "%s not a valid path to oplrun"%oplrun_path)
    if tu.development_deployed_environment:
        if "LD_LIBRARY_PATH" not in os.environ.keys():
            os.environ["LD_LIBRARY_PATH"] = os.path.abspath(os.path.join(oplrun_path,'..'))
        elif not oplrun_path in os.environ["LD_LIBRARY_PATH"]:
            os.environ["LD_LIBRARY_PATH"] = os.path.abspath(os.path.join(oplrun_path,'..')) + \
                                            ":" + os.environ["LD_LIBRARY_PATH"]
    try:
        output = subprocess.check_output([oplrun_path, mod_file, datfile], stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as err:
        if tu.development_deployed_environment:
            raise Exception("oplrun failed to complete: " + err.output)
        output = err.output
    with open(output_txt, "w") as f:
        f.write(output)
    if not os.path.isfile(results_dat):
        print("%s is not a valid file. A solution was likely not generated. Check 'output.txt' for details."%results_dat)
        return None
    with open(results_dat, "r") as f:
        output = f.read()
    if post_solve:
        post_solve()
    soln_tdf = _unfix_fields_with_opl_keywords(soln_tdf)
    return read_opl_text(soln_tdf, output, False)

_can_run_oplrun_tests = os.path.isfile(os.path.join(_code_dir(),"oplrun_path.txt"))

def create_opl_text(tdf, tic_dat, infinity=INFINITY):
    """
    Generate a OPL .dat string from a TicDat object
    :param tdf: A TicDatFactory defining the schema
    :param tic_dat: A TicDat object consistent with tdf
    :param infinity: A number used to represent infinity in OPL
    :return: A string consistent with the OPL .dat format
    """
    msg = []
    verify(tdf.good_tic_dat_object(tic_dat, msg.append),
           "tic_dat not a good object for this factory : %s"%"\n".join(msg))
    verify(not tdf.generator_tables, "doesn't work with generator tables.")
    verify(not tdf.generic_tables, "doesn't work with generic tables. (not yet - will add ASAP as needed) ")
    dict_with_lists = defaultdict(list)
    dict_tables = {t for t,pk in tdf.primary_key_fields.items() if pk}
    for t in dict_tables:
        for k,r in getattr(tic_dat, t).items():
            row = list(k) if containerish(k) else [k]
            for f in tdf.data_fields.get(t, []):
                row.append(r[f])
            dict_with_lists[t].append(row)
    for t in set(tdf.all_tables).difference(dict_tables):
        for r in getattr(tic_dat, t):
            row = [r[f] for f in tdf.data_fields[t]]
            dict_with_lists[t].append(row)

    rtn = ""
    for i, (t,l) in enumerate(dict_with_lists.items()):
        rtn += "\n" if i > 0 else ""
        rtn += "%s = {"%(tdf.opl_prepend + t)
        if len(l[0]) > 1:
            rtn += "\n"
        for x in range(len(l)):
            r = l[x]
            if len(r) > 1:
                rtn += "<"
            for i,v in enumerate(r):
                rtn += ('"%s"'%v if stringish(v) else (str(infinity) if float('inf') == v else str(v))) + (", " if i < len(r)-1 else "")
            if len(r) == 1 and len(l)-1 != x:
                rtn += ', '
            if len(r) > 1:
                rtn += ">\n"
        rtn += "};\n"

    return rtn

def create_opl_mod_text(tdf):
    """
    Generate a OPL .mod string from a TicDat object for diagnostic purposes
    :param tdf: A TicDatFactory defining the input schema
    :return: A string consistent with the OPL .mod input format
    """
    return _create_opl_mod_text(tdf, False)

def create_opl_mod_output_text(tdf):
    """
    Generate a OPL .mod string from a TicDat object for diagnostic purposes
    :param tdf: A TicDatFactory defining the input schema
    :return: A string consistent with the OPL .mod output format
    """
    return _create_opl_mod_text(tdf, True)

def _create_opl_mod_text(tdf, output):
    verify(not _find_case_space_duplicates(tdf), "There are case space duplicate field names in the schema.")
    verify(not tdf.generator_tables, "Input schema error - doesn't work with generator tables.")
    verify(not tdf.generic_tables, "Input schema error - doesn't work with generic tables. (not yet - will \
            add ASAP as needed) ")
    tdf = _fix_fields_with_opl_keywords(tdf)
    rtn = ''
    dict_tables = {t for t, pk in tdf.primary_key_fields.items() if pk}
    verify(set(dict_tables) == set(tdf.all_tables), "not yet handling non-PK tables of any sort")

    prepend = getattr(tdf, "opl_prepend", "")
    def _get_type(data_types, table, field, is_pk):
        try:
            return "float" if data_types[table][field].number_allowed else "string"
        except KeyError:
            if is_pk:
                return "string"
            return "float"

    def get_table_as_mod_text(tdf, tbn, output):
        rtn = ''
        sig = '{}' if output else '...'
        if len(tdf.primary_key_fields[tbn]) is 1 and len(tdf.data_fields[tbn]) is 0:
            rtn = "{" + _get_type(tdf.data_types, tbn, tdf.primary_key_fields[tbn][0], True) + "} " + \
                  prepend + tbn + " = " + sig + ";\n\n"
        else:
            rtn += "tuple " + prepend + tbn + "_type\n{"
            for pk in tdf.primary_key_fields[tbn]:
                pk_m = pk.replace(' ', '_').lower()
                rtn += "\n\tkey " + _get_type(tdf.data_types, tbn, pk, True) + " " + pk_m + ";"
            for df in tdf.data_fields[tbn]:
                df_m = df.replace(' ', '_').lower()
                rtn += "\n\t" + _get_type(tdf.data_types, tbn, df, False) + " " + df_m + ";"
            rtn += "\n};\n\n{" + prepend + tbn + "_type} " + prepend + tbn + "=" + sig + ";\n\n"
        return rtn

    for t in dict_tables:
        rtn += get_table_as_mod_text(tdf, t, output)
    return rtn

def read_opl_text(tdf,text, commaseperator = True):
    """
    Read an OPL .dat string
    :param tdf: A TicDatFactory defining the schema
    :param text: A string consistent with the OPL .dat format
    :return: A TicDat object consistent with tdf
    """
    verify(stringish(text), "text needs to be a string")
    # probably want to verify something about the ticdat factory, look at the wiki
    dict_with_lists = defaultdict(list)
    NONE, TABLE, ROW, ROWSTRING, ROWNUM, FIELD, STRING,  NUMBER = 1, 2, 3, 4, 5, 6, 7, 8
    mode = NONE
    field = ''
    table_name = ''
    row = []

    def to_number(st, pos):
        try:
            return float(st)
        except ValueError:
            verify(False,
                   "Badly formatted string - Field '%s' is not a valid number. Character position [%s]." % (st, pos))

    for i,c in enumerate(text):
        if mode not in [STRING, ROWSTRING] and (c.isspace() or c == '{' or c == ';'):
            if mode in [ROWNUM,FIELD] and not commaseperator:
                c = ','
            else:
                continue
        if mode in [STRING, ROWSTRING]:
            if c == '"':
                if text[i-1] == '\\':
                    field = field[:-1] + '"'
                else:
                    if mode is ROWSTRING:
                        row.append(field)
                        field = ''
                        verify(len(row) == len((dict_with_lists[table_name] or [row])[0]),
                               "Inconsistent row lengths found for table %s" % table_name)
                        dict_with_lists[table_name].append(row)
                        row = []
                        mode = TABLE
                    else:
                        mode = FIELD
            else:
                field += c
        elif c == '=':
            verify(mode is NONE, "Badly formatted string, unrecognized '='. Character position [%s]"%i)
            verify(len(table_name) > 0, "Badly formatted string, table name can't be blank. Character position [%s]"%i)
            verify(table_name not in dict_with_lists.keys(), "Can't have duplicate table name. [Character position [%s]"%i)
            dict_with_lists[table_name] = []
            mode = TABLE
        elif c == '<':
            verify(mode is TABLE, "Badly formatted string, unrecognized '<'. Character position [%s]"%i)
            mode = ROW

        elif c == ',':
            verify(mode in [ROW, FIELD, NUMBER, ROWNUM, TABLE], "Badly formatted string, unrecognized ','. \
                                                                    Character position [%s]"%i)
            if mode is TABLE:
                continue
            if mode is ROWNUM:
                field = to_number(field,i)
                row.append(field)
                field = ''
                verify(len(row) == len((dict_with_lists[table_name] or [row])[0]),
                       "Inconsistent row lengths found for table %s" % table_name)
                dict_with_lists[table_name].append(row)
                row = []
                mode = TABLE
            else:
                if mode is NUMBER:
                    field = to_number(field,i)
                row.append(field)
                field = ''
                mode = ROW

        elif c == '"':
            verify(mode in [ROW, TABLE], "Badly formatted string, unrecognized '\"'. Character position [%s]"%i)
            if mode is ROW:
                mode = STRING
            if mode is TABLE:
                mode = ROWSTRING

        elif c == '}':
            verify(mode in [TABLE, ROWNUM], "Badly formatted string, unrecognized '}'. Character position [%s]"%i)
            if mode is ROWNUM:
                field = to_number(field,i)
                row.append(field)
                field = ''
                verify(len(row) == len((dict_with_lists[table_name] or [row])[0]),
                       "Inconsistent row lengths found for table %s" % table_name)
                dict_with_lists[table_name].append(row)
            row = []
            table_name = ''
            mode = NONE

        elif c == '>':
            verify(mode in [ROW, FIELD, NUMBER], "Badly formatted string, unrecognized '>'. \
                                                                    Character position [%s]"%i)
            if mode is NUMBER:
                field = to_number(field,i)
                mode = FIELD
            if mode is FIELD:
                row.append(field)
                field = ''
            verify(len(row) == len((dict_with_lists[table_name] or [row])[0]),
                   "Inconsistent row lengths found for table %s"%table_name)
            dict_with_lists[table_name].append(row)
            row = []
            mode = TABLE
        else:
            verify(mode in [NONE, ROW, ROWNUM, FIELD, NUMBER], "Badly formatted string, \
                                                                    unrecognized '%s'. Character position [%s]"%(c,i))
            if mode is NONE:
                table_name += c
            elif mode is TABLE:
                mode = ROWNUM
                field += c
            else:
                mode = NUMBER
                field += c
    assert not find_duplicates_from_dict_ticdat(tdf, dict_with_lists), \
            "duplicates were found - if asserts are disabled, duplicate rows will overwrite"

    return tdf.TicDat(**{k.replace(tdf.opl_prepend,"",1):v for k,v in dict_with_lists.items()})