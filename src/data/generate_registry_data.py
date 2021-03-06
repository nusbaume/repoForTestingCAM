#!/usr/bin/env python

"""
Read CAM registry and produce data and metadata files

To run doctest on this file: python -m doctest generate_registry_data.py
"""

# Python library imports
# NB: ET is used in doctests which are not recognized by pylint
import xml.etree.ElementTree as ET # pylint: disable=unused-import
import os
import os.path
import re
import argparse
import sys
import logging
from collections import OrderedDict

# Find and include the ccpp-framework scripts directory
# Assume we are in <CAMROOT>/src/data and SPIN is in <CAMROOT>/ccpp_framework
__CURRDIR = os.path.abspath(os.path.dirname(__file__))
__CAMROOT = os.path.abspath(os.path.join(__CURRDIR, os.pardir, os.pardir))
__SPINSCRIPTS = os.path.join(__CAMROOT, "ccpp_framework", 'scripts')
if __SPINSCRIPTS not in sys.path:
    sys.path.append(__SPINSCRIPTS)
# end if

# CCPP framework imports
# pylint: disable=wrong-import-position
from parse_tools import validate_xml_file, read_xml_file
from parse_tools import find_schema_file, find_schema_version
from parse_tools import init_log, CCPPError, ParseInternalError
from fortran_tools import FortranWriter
# pylint: enable=wrong-import-position

###############################################################################
def convert_to_long_name(standard_name):
###############################################################################
    """Convert <standard_name> to an easier-to-read string
    NB: While this is similar to the CCPP conversion, they do not have to
        have the same form or functionality"""
    return standard_name[0].upper() + re.sub("_", " ", standard_name[1:])

###############################################################################
def write_ccpp_table_header(name, outfile):
###############################################################################
    """Write the standard Fortran comment block for a CCPP header
    (module, type, scheme)."""
    outfile.write(r"!> \section arg_table_{}  Argument Table".format(name), 0)
    outfile.write(r"!! \htmlinclude {}.html".format(name), 0)

###############################################################################
class TypeEntry:
###############################################################################
    "Simple type to capture a type and its source module name"

    def __init__(self, ttype, module, ddt=None):
        """Initialize TypeEntry"""
        self.__type = ttype
        self.__module = module
        self.__ddt = ddt # The actual DDT object, if <ttype> is a DDT

    @property
    def type_type(self):
        """Return type string"""
        return self.__type

    @property
    def module(self):
        """Return module where this type is defined or None for an intrinsic"""
        return self.__module

    @property
    def ddt(self):
        """Return DDT object, or None"""
        return self.__ddt

###############################################################################
class TypeRegistry(dict):
###############################################################################
    """Dictionary of known types. DDTs are associated with the module
    where they are defined"""

    def __init__(self):
        """Initialize TypeRegistry object with intrinsic Fortran types"""
        super(TypeRegistry, self).__init__()
        self['character'] = TypeEntry('character', None)
        self['complex'] = TypeEntry('complex', None)
        self['integer'] = TypeEntry('integer', None)
        self['logical'] = TypeEntry('logical', None)
        self['real'] = TypeEntry('real', None)

    def known_type(self, test_type):
        """Return type and a module name where <test_type> is defined
        or None if <test_type> is not in this TypeRegistry"""
        ttype = test_type.lower()
        if ttype in self:
            return self[ttype]
        # end if
        return None

    def add_type(self, new_type, type_module, type_ddt=None):
        """Add a new type, <new_type>, defined in <type_module> to
        this registry"""
        ttype = new_type
        if ttype in self:
            emsg = 'Trying to add {} to registry, already defined in {}'
            raise ValueError(emsg.format(new_type, self[ttype].module))
        # end if
        self[ttype] = TypeEntry(new_type, type_module, type_ddt)

###############################################################################
class VarBase(object):
###############################################################################
    """VarBase contains elements common to variables, arrays, and
    array elements."""

    __pointer_def_init = "NULL()"
    __pointer_type_str = "pointer"

    def __init__(self, elem_node, local_name, dimensions, known_types,
                 type_default, units_default="",
                 kind_default='', alloc_default='none'):
        self.__local_name = local_name
        self.__dimensions = dimensions
        self.__units = elem_node.get('units', default=units_default)
        ttype = elem_node.get('type', default=type_default)
        self.__type = known_types.known_type(ttype)
        self.__kind = elem_node.get('kind', default=kind_default)
        self.__standard_name = elem_node.get('standard_name')
        self.__long_name = ''
        self.__initial_value = ''
        self.__ic_names = None
        self.__allocatable = elem_node.get('allocatable', default=alloc_default)
        if self.__allocatable == "none":
            self.__allocatable = ""
        # end if
        if self.__type:
            # We cannot have a kind property with a DDT type2
            if self.is_ddt and self.kind:
                emsg = "kind attribute illegal for DDT type {}"
                raise CCPPError(emsg.format(self.var_type))
            # end if (else this type is okay)
        else:
            emsg = '{} is an unknown Variable type, {}'
            raise CCPPError(emsg.format(local_name, ttype))
        # end if
        for attrib in elem_node:
            if attrib.tag == 'long_name':
                self.__long_name = attrib.text
            elif attrib.tag == 'initial_value':
                self.__initial_value = attrib.text
            elif attrib.tag == 'ic_file_input_names':
                #Separate out string into list:
                self.__ic_names = [x.strip() for x in attrib.text.split(' ') if x]

            # end if (just ignore other tags)
        # end for
        if ((not self.initial_value) and
            (self.allocatable == VarBase.__pointer_type_str)):
            self.__initial_value = VarBase.__pointer_def_init
        # end if

    def write_metadata(self, outfile):
        """Write out this variable as CCPP metadata"""
        outfile.write('[ {} ]\n'.format(self.local_name))
        outfile.write('  {} = {}\n'.format('standard_name', self.standard_name))
        if self.long_name:
            outfile.write('  {} = {}\n'.format('long_name', self.long_name))
        # end if
        outfile.write('  {} = {}\n'.format('units', self.units))
        if self.is_ddt:
            outfile.write('  {} = {}\n'.format('ddt_type', self.var_type))
        elif self.kind:
            outfile.write('  {} = {} | {} = {}\n'.format('type', self.var_type,
                                                         'kind', self.kind))
        else:
            outfile.write('  {} = {}\n'.format('type', self.var_type))
        # end if
        outfile.write('  {} = {}\n'.format('dimensions',
                                           self.dimension_string))

    def write_initial_value(self, outfile, indent, init_var, ddt_str):
        """Write the code for the initial value of this variable
        and/or one of its array elements."""
        #Check if variable has associated array index
        #local string:
        if hasattr(self, 'local_index_name_str'):
            #Then write variable with local index name:
            var_name = '{}{}'.format(ddt_str, self.local_index_name_str)
        else:
            #Otherwise, use regular local variable name:
            var_name = '{}{}'.format(ddt_str, self.local_name)
        if self.allocatable == VarBase.__pointer_type_str:
            if self.initial_value == VarBase.__pointer_def_init:
                init_val = ''
            else:
                init_val = self.initial_value
            # end if
        else:
            init_val = self.initial_value
        # end if
        if not init_val:
            if self.var_type.lower() == 'real':
                init_val = 'nan'
            elif self.var_type.lower() == 'integer':
                init_val = 'HUGE(1)'
            elif self.var_type.lower() == 'character':
                init_val = '""'
            else:
                init_val = ''
            # end if
        # end if
        if init_val:
            outfile.write("if ({}) then".format(init_var), indent)
            outfile.write("{} = {}".format(var_name, init_val), indent+1)
            outfile.write("end if", indent)
            # end if
        # end if

    @property
    def local_name(self):
        """Return the local (variable) name for this variable"""
        return self.__local_name

    @property
    def standard_name(self):
        """Return the standard_name for this variable"""
        return self.__standard_name

    @property
    def units(self):
        """Return the units for this variable"""
        return self.__units

    @property
    def kind(self):
        """Return the kind for this variable"""
        return self.__kind

    @property
    def allocatable(self):
        """Return the allocatable attribute (if any) for this variable"""
        return self.__allocatable

    @property
    def dimensions(self):
        """Return the dimensions for this variable"""
        return self.__dimensions

    @property
    def dimension_string(self):
        """Return the dimension_string for this variable"""
        return '(' + ', '.join(self.dimensions) + ')'

    @property
    def long_name(self):
        """Return the long_name for this variable"""
        return self.__long_name

    @property
    def initial_value(self):
        """Return the initial_value for this variable"""
        return self.__initial_value

    @property
    def ic_names(self):
        """Return list of possible Initial Condition (IC) file input names"""
        #Assume ic_names exists:
        return self.__ic_names

    @property
    def module(self):
        """Return the module where this variable is defined"""
        return self.__type.module

    @property
    def var_type(self):
        """Return the variable type for this variable"""
        return self.__type.type_type

    @property
    def is_ddt(self):
        """Return True iff this variable is a derived type"""
        return self.__type.ddt

###############################################################################
class ArrayElement(VarBase):
###############################################################################
    """Documented array element of a registry Variable"""

    def __init__(self, elem_node, parent_name, dimensions, known_types,
                 parent_type, parent_kind, parent_units, parent_alloc, vdict):
        """Initialize the Arary Element information by identifying its
        metadata properties
        """

        self.__parent_name = parent_name
        self.__index_name = elem_node.get('index_name')
        pos = elem_node.get('index_pos')

        # Check to make sure we know about this index
        var = vdict.find_variable_by_standard_name(self.index_name)
        if not var:
            emsg = "Unknown array index, '{}', in '{}'"
            raise CCPPError(emsg.format(self.index_name, parent_name))
        # end if
        # Find the location of this element's index
        found = False
        my_dimensions = list()
        my_index = list()
        my_local_index = list()
        for dim_ind, dim in enumerate(dimensions):
            if dimensions[dim_ind] == pos:
                found = True
                my_index.append(self.index_name)
                my_local_index.append(var.local_name)
            else:
                my_index.append(':')
                my_local_index.append(':')
                my_dimensions.append(dim)
            # end if
        # end for
        if found:
            self.__index_string = ','.join(my_index)
            #write array string with local variable index name,
            #instead of the standard variable index name.
            #This is used to write initialization code in fortran
            #with the correct index variable name:
            local_index_string = ','.join(my_local_index)
            self.__local_index_name_str = \
                '{}({})'.format(parent_name, local_index_string)
        else:
            emsg = "Cannot find element dimension, '{}' in {}({})"
            raise CCPPError(emsg.format(self.index_name, parent_name,
                                        ', '.join(dimensions)))
        # end if
        local_name = '{}({})'.format(parent_name, self.index_string)
        super(ArrayElement, self).__init__(elem_node, local_name, my_dimensions,
                                           known_types, parent_type,
                                           units_default=parent_units,
                                           kind_default=parent_kind,
                                           alloc_default=parent_alloc)
    @property
    def index_name(self):
        """Return the standard name of this array element's index value"""
        return self.__index_name

    @property
    def local_index_name_str(self):
        """
        Return the array element's name, but with the local name for the
        index instead of the standard name
        """
        return self.__local_index_name_str

    @property
    def index_string(self):
        """Return the metadata string for locating this element's index in
        its parent array"""
        return self.__index_string

    @property
    def parent_name(self):
        """Return this element's parent's local name"""
        return self.__parent_name

###############################################################################
class Variable(VarBase):
###############################################################################
    # pylint: disable=too-many-instance-attributes
    """Registry variable
    >>> Variable(ET.fromstring('<variable kind="kind_phys" local_name="u" standard_name="east_wind" type="real" units="m s-1"><dimensions>ccpp_constant_one:horizontal_dimension:two</dimensions></variable>'), TypeRegistry(), VarDict("foo", "module", None), None) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Illegal dimension string, ccpp_constant_one:horizontal_dimension:two, in u, step not allowed.
    >>> Variable(ET.fromstring('<variable kind="kind_phys" local_name="u" standard_name="east_wind" type="real" units="m s-1"><dims>horizontal_dimension</dims></variable>'), TypeRegistry(), VarDict("foo", "module", None), None) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Unknown Variable content, dims
    >>> Variable(ET.fromstring('<variable kkind="kind_phys" local_name="u" standard_name="east_wind" type="real" units="m s-1"></variable>'), TypeRegistry(), VarDict("foo", "module", None), None) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Bad variable attribute, 'kkind', for 'u'
    >>> Variable(ET.fromstring('<variable kind="kind_phys" local_name="u" standard_name="east_wind" type="real" units="m s-1" allocatable="target"><dimensions>horizontal_dimension vertical_dimension</dimensions></variable>'), TypeRegistry(), VarDict("foo", "module", None), None) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Dimension, 'vertical_dimension', not found for 'u'
    """

    # Constant dimensions
    __CONSTANT_DIMENSIONS = {'ccpp_constant_one' : 1, 'ccpp_constant_zero' : 0}

    __VAR_ATTRIBUTES = ["access", "allocatable", "dycore", "extends",
                        "kind", "local_name", "name", "standard_name",
                        "type", "units", "version"]

    def __init__(self, var_node, known_types, vdict, logger):
        # pylint: disable=too-many-locals
        """Initialize a Variable from registry XML"""
        self.__elements = list()
        local_name = var_node.get('local_name')
        allocatable = var_node.get('allocatable', default="none")
        # Check attributes
        for att in var_node.attrib:
            if att not in Variable.__VAR_ATTRIBUTES:
                emsg = "Bad variable attribute, '{}', for '{}'"
                raise CCPPError(emsg.format(att, local_name))
            # end if
        # end for
        ttype = var_node.get('type')
        self.__access = var_node.get('access', default='public')
        if self.__access == "protected":
            self.__access = "public"
            self.__protected = True
        else:
            self.__protected = False
        # end if
        my_dimensions = list()
        self.__def_dims_str = ""
        for attrib in var_node:
            if attrib.tag == 'dimensions':
                my_dimensions = [x.strip() for x in attrib.text.split(' ') if x]
                def_dims = list() # Dims used for variable declarations
                for dim in my_dimensions:
                    if dim.count(':') > 1:
                        emsg = "Illegal dimension string, '{},' in '{}'"
                        emsg += ', step not allowed.'
                        raise CCPPError(emsg.format(dim, local_name))
                    # end if
                    if allocatable in ("", "parameter", "target"):
                        # We need to find a local variable for every dimension
                        dimstrs = [x.strip() for x in dim.split(':')]
                        ldimstrs = list()
                        for ddim in dimstrs:
                            lname = Variable.constant_dimension(ddim)
                            if not lname:
                                var = vdict.find_variable_by_standard_name(ddim)
                                if var:
                                    lname = var.local_name
                                # end if
                            # end if
                            if not lname:
                                emsg = "Dimension, '{}', not found for '{}'"
                                raise CCPPError(emsg.format(ddim,
                                                            local_name))
                            # end if
                            ldimstrs.append(lname)
                        # end for
                        def_dims.append(':'.join(ldimstrs))
                    else:
                        # We need to allocate this array
                        def_dims.append(':')
                    # end if
                # end for
                if def_dims:
                    self.__def_dims_str = '(' + ', '.join(def_dims) + ')'
                # end if

            elif attrib.tag == 'long_name':
                pass # picked up in parent
            elif attrib.tag == 'initial_value':
                pass # picked up in parent
            elif attrib.tag == 'element':
                pass # picked up in parent
            elif attrib.tag == 'ic_file_input_names':
                pass # picked up in parent
            else:
                emsg = "Unknown Variable content, '{}'"
                raise CCPPError(emsg.format(attrib.tag))
            # end if
        # end for
        # Initialize the base class
        super(Variable, self).__init__(var_node, local_name,
                                       my_dimensions, known_types, ttype)
        for attrib in var_node:
            # Second pass, only process array elements
            if attrib.tag == 'element':
                self.__elements.append(ArrayElement(attrib, local_name,
                                                    my_dimensions, known_types,
                                                    ttype, self.kind,
                                                    self.units, allocatable,
                                                    vdict))

            # end if (all other processing done above)
        # end for
        # Some checks
        if (self.allocatable == 'parameter') and (not self.initial_value):
            emsg = "parameter, '{}', does not have an initial value"
            raise CCPPError(emsg.format(local_name))
        # end if
        # Maybe fix up type string
        if self.module:
            self.__type_string = 'type({})'.format(self.var_type)
        elif self.kind:
            self.__type_string = '{}({})'.format(self.var_type, self.kind)
        else:
            self.__type_string = '{}'.format(self.var_type)
        # end if
        if logger:
            dmsg = 'Found registry Variable, {} ({})'
            logger.debug(dmsg.format(self.local_name, self.standard_name))
        # end if

    def write_metadata(self, outfile):
        """Write out this variable as CCPP metadata"""
        if self.access != "private":
            super(Variable, self).write_metadata(outfile)
            if (self.allocatable == "parameter") or self.protected:
                outfile.write('  protected = True\n')
            # end if
            for element in self.__elements:
                element.write_metadata(outfile)
            # end for
        # end if

    def write_definition(self, outfile, access, indent,
                         maxtyp=0, maxacc=0, maxall=0, has_protect=False):
        """Write the definition for this variable to <outfile>
        with indent, <indent>.
        <access> is the current public/private scope.
        <maxtyp> is the maximum padding to use for the type declaration.
        <maxacc> is the maximum padding to use for any access specification.
        <maxall> is the maximum padding to use for any allocation-type spec.
        <has_protect> specifies whether to leave space for a protected string.
            Note that if <has_protect> is False, output of the protected
            attribute is suppressed (e.g., for a DDT, even 'protected'
            variables cannot have the protected attribute.
        """
        # Protected string
        if has_protect:
            if self.protected:
                pro_str = "protected"
                has_pro = True
            else:
                pro_str = "         "
                has_pro = False
            # end if
        else:
            pro_str = ""
            has_pro = False
        # end if
        # Allocation string
        if self.allocatable:
            apad = ' '*max(0, maxall - len(self.allocatable))
            if has_pro:
                all_str = self.allocatable + ", " + apad
            else:
                all_str = self.allocatable + apad
            # end if
            have_all = True
        else:
            all_str = ' '*(maxall + 2)
            have_all = False
        # end if
        # Access string
        if self.access == access:
            acc_str = ' '*(maxacc + 2)
            have_vis = False
        else:
            vpad = ' '*max(0, maxacc - len(self.access))
            if have_all or has_pro:
                acc_str = self.access + ", " + vpad
            else:
                acc_str = self.access + vpad
            # end if
            have_vis = True
        # end if
        # Type string
        tpad = ' '*max(0, maxtyp - len(self.type_string))
        if have_all or have_vis or has_pro:
            tpad = ", " + tpad
        # end if
        type_str = self.type_string + tpad
        # Initial value
        if self.initial_value:
            if self.allocatable == "pointer":
                init_str = " => {}".format(self.initial_value)
            elif not (self.allocatable[0:11] == 'allocatable'):
                init_str = " = {}".format(self.initial_value)
            # end if (no else, do not initialize allocatable fields)
        else:
            init_str = ""
        # end if
        if self.long_name:
            comment = ' ! ' + self.local_name + ": " + self.long_name
        else:
            comment = (' ! ' + self.local_name + ": " +
                       convert_to_long_name(self.standard_name))
        # end if
        outfile.write(comment, indent)
        outfile.write("{}{}{}{} :: {}{}{}".format(type_str, acc_str,
                                                  all_str, pro_str,
                                                  self.local_name,
                                                  self.__def_dims_str,
                                                  init_str), indent)

    def write_allocate_routine(self, outfile, indent,
                               init_var, reall_var, ddt_str):
        """Write the code to allocate and initialize this Variable
        <init_var> is a string to use to write initialization test code.
        <reall_var> is a string to use to write reallocate test code.
        <ddt_str> is a prefix string (e.g., state%).
        <known_types> is a TypeRegistry.
        """
        # Be careful about dimensions, scalars have none, not '()'
        if self.dimensions:
            dimension_string = self.dimension_string
        else:
            dimension_string = ''
        # end if
        my_ddt = self.is_ddt
        if my_ddt: # This is a DDT object, allocate entries
            subi = indent
            sub_ddt_str = '{}{}%'.format(ddt_str, self.local_name)
            if dimension_string:
                subi += 1
                emsg = "Arrays of DDT objects not implemented"
                raise ParseInternalError(emsg)
            # end if
            for var in my_ddt.variable_list():
                var.write_allocate_routine(outfile, subi,
                                           init_var, reall_var, sub_ddt_str)
        else:
            # Do we need to allocate this variable?
            lname = '{}{}'.format(ddt_str, self.local_name)
            if self.allocatable == "pointer":
                all_type = 'associated'
            elif self.allocatable == "allocatable":
                all_type = 'allocated'
            else:
                all_type = ''
            # end if
            if all_type:
                outfile.write("if ({}({})) then".format(all_type, lname),
                              indent)
                outfile.write("if ({}) then".format(reall_var), indent+1)
                outfile.write("deallocate({})".format(lname), indent+2)
                if self.allocatable == "pointer":
                    outfile.write("nullify({})".format(lname), indent+2)
                # end if
                outfile.write("else", indent+1)
                emsg = 'subname//": {} is already {}'.format(lname, all_type)
                emsg += ', cannot allocate"'
                outfile.write("call endrun({})".format(emsg), indent+2)
                outfile.write("end if", indent+1)
                outfile.write("end if", indent)
                outfile.write("allocate({}{})".format(lname, dimension_string),
                              indent)
            # end if
            if self.allocatable != "parameter":
                # Initialize the variable
                self.write_initial_value(outfile, indent, init_var, ddt_str)
                for elem in self.__elements:
                    if elem.initial_value:
                        elem.write_initial_value(outfile, indent,
                                                 init_var, ddt_str)
                    # end if
                # end for
            # end if

    @classmethod
    def constant_dimension(cls, dim):
        """Return dimension value if <dim> is a constant dimension, else None"""
        if dim.lower() in Variable.__CONSTANT_DIMENSIONS:
            dim_val = Variable.__CONSTANT_DIMENSIONS[dim.lower()]
        else:
            dim_val = None
        # end if
        return dim_val

    @property
    def type_string(self):
        """Return the type_string for this variable"""
        return self.__type_string

    @property
    def access(self):
        """Return the access attribute for this variable"""
        return self.__access

    @property
    def protected(self):
        """Return True iff this variable is protected"""
        return self.__protected

    @property
    def elements(self):
        """Return elements list for this variable"""
        return self.__elements

###############################################################################
class VarDict(OrderedDict):
###############################################################################
    """Ordered dictionary of registry variables"""

    def __init__(self, name, ttype, logger):
        """Initialize a registry variable dictionary"""
        super(VarDict, self).__init__()
        self.__name = name
        self.__type = ttype
        self.__logger = logger
        self.__standard_names = list()
        self.__dimensions = set() # All known dimensions for this dictionary

    @property
    def name(self):
        """Return the name of this dictionary (usually the module name)"""
        return self.__name

    @property
    def module_type(self):
        """Return the module type (e.g., host, module) for this dictionary"""
        return self.__type

    @property
    def known_dimensions(self):
        """Return the set of known dimensions for this dictionary"""
        return self.__dimensions

    def add_variable(self, newvar):
        """Add a variable if it does not conflict with existing entries"""
        local_name = newvar.local_name
        std_name = newvar.standard_name
        if local_name.lower() in self:
            # We already have a matching variable, error!
            emsg = "duplicate variable local_name, '{}', in {}"
            ovar = self[local_name]
            if (ovar is not None) and (ovar.standard_name != std_name):
                emsg2 = ", already defined with standard_name, '{}'"
                emsg += emsg2.format(ovar.standard_name)
            # end if
            raise CCPPError(emsg.format(local_name, self.name))
        # end if
        if std_name.lower() in self.__standard_names:
            # We have a standard name collision, error!
            emsg = "duplicate variable standard_name, '{}' from '{}' in '{}'"
            ovar = None
            for testvar in self.variable_list():
                if testvar.standard_name.lower() == std_name.lower():
                    ovar = testvar
                    break
                # end if
            # end for
            if ovar is not None:
                emsg2 = ", already defined with local_name, '{}'"
                emsg += emsg2.format(ovar.local_name)
            # end if
            raise CCPPError(emsg.format(std_name, local_name, self.name))
        # end if
        self[local_name.lower()] = newvar
        self.__standard_names.append(std_name.lower())
        for dim in newvar.dimensions:
            dimstrs = [x.strip() for x in dim.split(':')]
            for ddim in dimstrs:
                lname = Variable.constant_dimension(ddim)
                if not lname:
                    self.__dimensions.add(dim.lower())
                # end if
            # end for
        # end for

    def find_variable_by_local_name(self, local_name):
        """Return this dictionary's variable matching local name, <local_name>.
        Return None if not found."""
        lname = local_name.lower()
        if lname in self:
            fvar = self[lname]
        else:
            if self.__logger:
                lmsg = 'Local name, {}, not found in {}'
                self.__logger.debug(lmsg.format(local_name, self.name))
            # end if
            fvar = None
        # end if
        return fvar

    def find_variable_by_standard_name(self, std_name):
        """Return this dictionary's variable matching standard name, <std_name>.
        Return None if not found."""
        sname = std_name.lower()
        fvar = None
        for var in self.variable_list():
            if sname == var.standard_name.lower():
                fvar = var
                break
            # end if
        # end for
        if (not fvar) and self.__logger:
            lmsg = 'Standard name, {}, not found in {}'
            self.__logger.debug(lmsg.format(std_name, self.name))
        # end if
        return fvar

    def remove_variable(self, std_name):
        """Remove <std_name> from the dictionary.
        Ignore if <std_name> is not in dict
        """
        var = self.find_variable_by_standard_name(std_name)
        if var:
            del self[var.local_name.lower()]
            # NB: Do not remove standard_name, it is still an error
        else:
            if self.__logger:
                lmsg = 'Cannot remove {} from {}, variable not found.'
                self.__logger.debug(lmsg.format(std_name, self.name))
            # end if
        # end if

    def variable_list(self):
        """Return a list of this dictionary's variables"""
        return self.values()

    def write_metadata(self, outfile):
        """Write out the variables in this dictionary as CCPP metadata"""
        outfile.write('[ccpp-arg-table]\n')
        outfile.write('  name = {}\n'.format(self.name))
        outfile.write('  type = {}\n'.format(self.module_type))
        for var in self.variable_list():
            var.write_metadata(outfile)
        # end if

    def write_definition(self, outfile, access, indent):
        """Write the definition for the variables in this dictionary to
        <outfile> with indent, <indent>.
        <access> is the current public/private scope.
        """
        maxtyp = 0
        maxacc = 0
        maxall = 0
        has_prot = False
        vlist = self.variable_list()

        for var in vlist:
            maxtyp = max(maxtyp, len(var.type_string))
            if var.access != access:
                maxacc = max(maxacc, len(var.access))
            # end if
            maxall = max(maxall, len(var.allocatable))
            has_prot = has_prot or var.protected
        # end for
        write_ccpp_table_header(self.name, outfile)
        for var in vlist:
            var.write_definition(outfile, access, indent, maxtyp=maxtyp,
                                 maxacc=maxacc, maxall=maxall,
                                 has_protect=has_prot)
        # end for

###############################################################################
class DDT:
###############################################################################
    """Registry DDT"""

    def __init__(self, ddt_node, known_types, var_dict, dycore, config, logger):
        """Initialize a DDT from registry XML (<ddt_node>)
        <var_dict> is the dictionary where variables referenced in <ddt_node>
        must reside. Each DDT variable is removed from <var_dict>

        >>> DDT(ET.fromstring('<ddt type="physics_state"><dessert>ice_cream</dessert></ddt>'), TypeRegistry(), VarDict("foo", "module", None), 'eul', None, None) #doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        CCPPError: Unknown DDT element type, 'dessert', in 'physics_state'
        """
        self.__type = ddt_node.get('type')
        self.__logger = logger
        self.__data = list()
        extends = ddt_node.get('extends', default=None)
        if extends is None:
            self.__extends = None
        else:
            self.__extends = known_types.known_type(extends)
        # end if
        if extends and (not self.__extends):
            emsg = ("DDT, '{}', extends type '{}', however, this type is "
                    "not known")
            raise CCPPError(emsg.format(self.ddt_type, extends))
        # end if
        self.__bindc = ddt_node.get('bindC', default=False)
        if self.__extends and self.__bindc:
            emsg = ("DDT, '{}', cannot have both 'extends' and 'bindC' "
                    "attributes")
            raise CCPPError(emsg.format(self.ddt_type))
        # end if
        self.__private = ddt_node.get('private', default=False)
        for attrib in ddt_node:
            if attrib.tag == 'data':
                varname = attrib.text
                include_var = True
                attrib_dycores = [x.strip().lower() for x in
                                  attrib.get('dycore', default="").split(',')
                                  if x]
                if attrib_dycores and (dycore not in attrib_dycores):
                    include_var = False
                # end if
                if include_var:
                    var = var_dict.find_variable_by_standard_name(varname)
                    if var:
                        self.__data.append(var)
                        var_dict.remove_variable(varname)
                    else:
                        emsg = ("Variable, '{}', not found for DDT, '{}', "
                                "in '{}'")
                        raise CCPPError(emsg.format(varname, self.ddt_type,
                                                    var_dict.name))
                    # end if
                # end if
            else:
                emsg = "Unknown DDT element type, '{}', in '{}'"
                raise CCPPError(emsg.format(attrib.tag, self.ddt_type))
            # end if
        # end for

    def variable_list(self):
        """Return the variable list for this DDT"""
        vlist = list(self.__data)
        if self.__extends:
            vlist.extend(self.__extends.ddt.variable_list())
        # end if
        return vlist

    def write_metadata(self, outfile):
        """Write out this DDT as CCPP metadata"""
        outfile.write('[ccpp-arg-table]\n')
        outfile.write('  name = {}\n'.format(self.ddt_type))
        outfile.write('  type = ddt\n')
        for var in self.__data:
            var.write_metadata(outfile)
        # end if

    def write_definition(self, outfile, access, indent):
        """Write out the Fortran definition for this DDT

        >>> DDT(ET.fromstring('<ddt type="physics_state">></ddt>'), TypeRegistry(), VarDict("foo", "module", None), 'eul', None, None).write_definition(None, 'public', 0) #doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        CCPPError: DDT, 'physics_state', has no member variables
        """
        # It is an error to have no member variables
        if not self.__data:
            emsg = "DDT, '{}', has no member variables"
            raise CCPPError(emsg.format(self.ddt_type))
        # end if
        my_acc = 'private' if self.private else 'public'
        if self.extends:
            acc_str = ', extends({})'.format(self.extends.type_type)
        elif self.bindC:
            acc_str = ', bind(C)'
        elif my_acc != access:
            acc_str = ', {}'.format(my_acc)
        else:
            acc_str = ''
        # end if
        # Write the CCPP header
        write_ccpp_table_header(self.ddt_type, outfile)
        # Write the type definition
        outfile.write("type{} :: {}".format(acc_str, self.ddt_type), indent)
        maxtyp = max([len(x.type_string) for x in self.__data])
        maxacc = max([len(x.access) for x in self.__data
                      if x.access != 'private'])
        maxall = max([len(x.allocatable) for x in self.__data])
        for var in self.__data:
            var.write_definition(outfile, my_acc, indent+1,
                                 maxtyp=maxtyp, maxacc=maxacc,
                                 maxall=maxall, has_protect=False)
        # end if
        outfile.write("end type {}\n".format(self.ddt_type), indent)

    @property
    def ddt_type(self):
        """Return this DDT's type"""
        return self.__type

    @property
    def private(self):
        """Return True iff this DDT is private"""
        return self.__private

    @property
    def extends(self):
        """Return this DDT's parent class, if any"""
        return self.__extends

    @property
    def bindC(self): # pylint: disable=invalid-name
        """Return True iff this DDT has the bind(C) attribute"""
        return self.__bindc

###############################################################################
class File:
###############################################################################
    """Object describing a file object in a registry file

    >>> File(ET.fromstring('<file name="physics_types" type="module"><use module="ccpp_kinds"/></file>'), TypeRegistry(), 'eul', "", None) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Illegal use entry, no reference
    >>> File(ET.fromstring('<file name="physics_types" type="module"><use reference="kind_phys"/></file>'), TypeRegistry(), 'eul', "", None) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Illegal use entry, no module
    >>> File(ET.fromstring('<file name="physics_types" type="module"><user reference="kind_phys"/></file>'), TypeRegistry(), 'eul', "", None) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Unknown registry File element, 'user'
    """

    # Some data for sorting dimension names
    __dim_order = {'horizontal_dimension' : 1,
                   'vertical_layer_dimension' : 2,
                   'vertical_interface_dimension' : 3,
                   'number_of_constituents' : 4}
    __min_dim_key = 5 # For sorting unknown dimensions

    def __init__(self, file_node, known_types, dycore, config, logger):
        """Initialize a File object from a registry node (XML)"""
        self.__var_dict = VarDict(file_node.get('name'), file_node.get('type'),
                                  logger)
        self.__name = file_node.get('name')
        self.__type = file_node.get('type')
        self.__known_types = known_types
        self.__ddts = OrderedDict()
        self.__use_statements = list()
        for obj in file_node:
            if obj.tag in ['variable', 'array']:
                newvar = Variable(obj, self.__known_types, self.__var_dict,
                                  logger)
                self.__var_dict.add_variable(newvar)
            elif obj.tag == 'ddt':
                newddt = DDT(obj, self.__known_types, self.__var_dict,
                             dycore, config, logger)
                dmsg = "Adding DDT {} from {} as a known type"
                dmsg = dmsg.format(newddt.ddt_type, self.__name)
                logging.debug(dmsg)
                self.__ddts[newddt.ddt_type] = newddt
                self.__known_types.add_type(newddt.ddt_type,
                                            self.__name, newddt)
            elif obj.tag == 'use':
                module = obj.get('module', default=None)
                if not module:
                    raise CCPPError('Illegal use entry, no module')
                # end if
                ref = obj.get('reference', default=None)
                if not ref:
                    raise CCPPError('Illegal use entry, no reference')
                # end if
                self.__use_statements.append((module, ref))
            else:
                emsg = "Unknown registry File element, '{}'"
                raise CCPPError(emsg.format(obj.tag))
            # end if
        # end for

    def write_metadata(self, outdir, logger):
        """Write out the variables in this file as CCPP metadata"""
        ofilename = os.path.join(outdir, "{}.meta".format(self.name))
        logger.info("Writing registry metadata file, {}".format(ofilename))
        with open(ofilename, "w") as outfile:
            # Write DDTs defined in this file
            for ddt in self.__ddts.values():
                ddt.write_metadata(outfile)
            # end if
            # Write Variables defined in this file
            self.__var_dict.write_metadata(outfile)
        # end with

    @classmethod
    def dim_sort_key(cls, dim_name):
        """Return an integer sort key for <dim_name>"""
        if dim_name not in File.__dim_order:
            key = File.__min_dim_key
            File.__min_dim_key += 1
            File.__dim_order[dim_name] = key
        # end if
        return File.__dim_order[dim_name]

    def write_source(self, outdir, indent, logger):
        """Write out source code for the variables in this file"""
        ofilename = os.path.join(outdir, "{}.F90".format(self.name))
        logger.info("Writing registry source file, {}".format(ofilename))
        with FortranWriter(ofilename, "w", indent=indent) as outfile:
            # Define the module header
            outfile.write('module {}\n'.format(self.name), 0)
            # Use statements (if any)
            module_list = list() # tuple of (module, type)
            for var in self.__var_dict.variable_list():
                mod = var.module
                if mod and (mod.lower() != self.name.lower()):
                    module_list.append((mod, var.var_type))
                # end if
            # end for
            # Add any DDT types
            for ddt in self.__ddts.values():
                for var in ddt.variable_list():
                    mod = var.module
                    if mod and (mod.lower() != self.name.lower()):
                        module_list.append((mod, var.var_type))
                    # end if
                # end for
            # end for
            # Add in any explicit use entries from the registry
            for ref in self.__use_statements:
                module_list.append(ref)
            # end if
            if module_list:
                maxlen = max([len(x[0]) for x in module_list])
            else:
                maxlen = 0 # Don't really need this
            # end if
            for module in module_list:
                mod = module[0]
                mtype = module[1]
                pad = ' '*(maxlen - len(mod))
                outfile.write('use {},{} only: {}'.format(mod, pad, mtype), 1)
            # end for
            # More boilerplate
            outfile.write("\nimplicit none\nprivate\n", 0)
            # Write DDTs defined in this file
            for ddt in self.__ddts.values():
                ddt.write_definition(outfile, 'private', 1)
            # end if
            # Write variable standard and input name arrays
            self.write_ic_names(outfile, indent-2, logger)
            # Write Variables defined in this file
            self.__var_dict.write_definition(outfile, 'private', 1)
            # Write data management subroutine declarations
            outfile.write('', 0)
            outfile.write('!! public interfaces', 0)
            outfile.write('public :: {}'.format(self.allocate_routine_name()),
                          1)
            # end of module header
            outfile.write("\nCONTAINS\n", 0)
            # Write data management subroutines
            self.write_allocate_routine(outfile)
            # end of module
            outfile.write('\nend module {}'.format(self.name), 0)

        # end with

    def allocate_routine_name(self):
        """Return the name of the allocate routine for this module"""
        return 'allocate_{}_fields'.format(self.name)

    def write_allocate_routine(self, outfile):
        """Write a subroutine to allocate all the data in this module"""
        subname = self.allocate_routine_name()
        args = list(self.__var_dict.known_dimensions)
        args.sort(key=File.dim_sort_key) # Attempt at a consistent interface
        init_var = 'set_init_val'
        args.append('{}_in'.format(init_var))
        reall_var = 'reallocate'
        args.append('{}_in'.format(reall_var))
        outfile.write('subroutine {}({})'.format(subname, ', '.join(args)), 1)
        # Use statements
        nanmods = 'nan => shr_infnan_nan, assignment(=)'
        outfile.write('use shr_infnan_mod,   only: {}'.format(nanmods), 2)
        outfile.write('use cam_abortutils,   only: endrun', 2)
        # Dummy arguments
        outfile.write('!! Dummy arguments', 2)
        for arg in args:
            if (init_var in arg) or (reall_var in arg):
                typ = 'logical'
                opt = ', optional, '
            else:
                typ = 'integer'
                opt = ',           '
            # end if
            outfile.write('{}{}intent(in) :: {}'.format(typ, opt, arg), 2)
        # end for
        outfile.write('', 0)
        outfile.write('!! Local variables', 2)
        outfile.write('logical                     :: {}'.format(init_var), 2)
        outfile.write('logical                     :: {}'.format(reall_var), 2)
        subn_str = 'character(len=*), parameter :: subname = "{}"'
        outfile.write(subn_str.format(subname), 2)
        outfile.write('', 0)
        outfile.write('! Set optional argument values', 2)
        outfile.write('if (present({}_in)) then'.format(init_var), 2)
        outfile.write('{iv} = {iv}_in'.format(iv=init_var), 3)
        outfile.write('else', 2)
        outfile.write('{} = .true.'.format(init_var), 3)
        outfile.write('end if', 2)
        outfile.write('if (present({}_in)) then'.format(reall_var), 2)
        outfile.write('{iv} = {iv}_in'.format(iv=reall_var), 3)
        outfile.write('else', 2)
        outfile.write('{} = .false.'.format(reall_var), 3)
        outfile.write('end if', 2)
        outfile.write('', 0)
        for var in self.__var_dict.variable_list():
            var.write_allocate_routine(outfile, 2, init_var, reall_var, '')
        # end for
        outfile.write('end subroutine {}'.format(subname), 1)

    def write_ic_names(self, outfile, indent, logger):
        """Write out the Initial Conditions (IC) file variable names arrays"""
        # pylint: disable=too-many-locals

        #Initialize variables:
        stdname_max_len = 0
        ic_name_max_num = 0

        #Create new (empty) list to store variables
        #with (IC) file input names:
        variable_list = list()

        #Loop over all DDTs in file:
        for ddt in self.__ddts.values():
            #Concatenate DDT variable list onto master list:
            variable_list.extend(ddt.variable_list())

        #Add file variable list to master list:
        variable_list.extend(list(self.__var_dict.variable_list()))

        #Loop through variable list to look for array elements:
        for var in list(variable_list):
            #Check if array elements are present:
            if var.elements:
                #If so, then loop over elements:
                for element in var.elements:
                    #Append element as new "variable" in variable list:
                    variable_list.append(element)

        #Determine max number of IC variable names:
        try:
            ic_name_max_num = max([len(var.ic_names) for var in variable_list if var.ic_names is not None])
        except ValueError:
            #If there is a ValueError, then likely no IC
            #input variable names exist, so print warning
            #and exit function:
            lmsg = "No '<ic_file_input_names>' tags exist in registry.xml" \
                   ", so no input variable name array will be created."
            logger.info(lmsg)
            return

        #Determine max standard name string length:
        try:
            stdname_max_len = max([len(var.standard_name) for var in variable_list])
        except ValueError:
            #If there are no proper standard names in the list,
            #then the registry was likely written incorrectly,
            #so print warning and exit function:
            lmsg = "No variable standard names were found that contain " \
                   "IC file input names.\nThus something is likely wrong " \
                   "with the registry file, or at least the placement of " \
                   "'<ic_file_input-names>' tags.\nGiven this, no input " \
                   "variable name array will be created."
            logger.info(lmsg)
            return

        #Determine total number of variables with file input (IC) names:
        num_vars_with_ic_names = len([var for var in variable_list if var.ic_names is not None])

        #Loop over variables in list:
        ic_name_max_len = self.find_ic_name_max_len(variable_list)

        #Create fake name with proper length:
        fake_ic_name = [" "*ic_name_max_len]

        #Create variable name array string lists:
        stdname_strs = list()
        ic_name_strs = list()

        #Initalize loop counter:
        lpcnt = 0

        #Loop over variables in list:
        for var in variable_list:

            #Check if variable actually has IC names:
            if var.ic_names is not None:

                #Add one to loop counter:
                lpcnt += 1

                #Create standard_name string with proper size,
                #and append to list:
                extra_spaces = " " * (stdname_max_len - len(var.standard_name))
                stdname_strs.append("'{}'".format(var.standard_name + extra_spaces))

                #Determine number of IC names for variable:
                ic_name_num = len(var.ic_names)

                #Create new (empty) list to store (IC) file
                #input names of variables with the correct
                #number of spaces to match character array
                #dimensions:
                ic_names_with_spaces = list()

                #Loop over possible input file (IC) names:
                for ic_name in var.ic_names:
                    #Create ic_name string with proper size:
                    extra_spaces = " " * (ic_name_max_len - len(ic_name))
                    #Add properly-sized name to list:
                    ic_names_with_spaces.append(ic_name + extra_spaces)

                #Create repeating list of empty, "fake" strings that
                #increases array to max size:
                if ic_name_max_num - ic_name_num != 0:
                    ic_names_with_spaces.append(fake_ic_name*(ic_name_max_num - ic_name_num))

                #Append new ic_names to string list:
                ic_name_strs.append(', '.join("'{}'".format(n) for n in ic_names_with_spaces))


        #Create new Fortran integer parameter to store number of variables with IC inputs:
        outfile.write("!Number of physics variables which can be read from"+ \
                      " Initial Conditions (IC) file:", indent)
        outfile.write("integer, public, parameter :: ic_var_num = {}".format(\
                      num_vars_with_ic_names), indent)

        #Add blank space:
        outfile.write("", 0)

        #Create another Fortran integer parameter to store max length of
        #registered variable standard name strings:
        outfile.write("!Max length of registered variable standard names:", indent)
        outfile.write("integer, public, parameter :: std_name_len = {}".format(\
                      stdname_max_len), indent)

        #Write a second blank space:
        outfile.write("", 0)

        #Create final Fortran integer parameter to store max length of
        #input variable name string:
        outfile.write("!Max length of input (IC) file variable names:", indent)
        outfile.write("integer, public, parameter :: ic_name_len = {}".format(\
                      ic_name_max_len), indent)

        #Write a third blank space:
        outfile.write("", 0)

        #Write starting declaration of input standard name array:
        declare_string = "character(len={}), public :: input_var_stdnames(ic_var_num) = (/ &".format(\
                         stdname_max_len)
        outfile.write(declare_string, indent)

        #Write standard names to fortran array:
        num_strs = len(stdname_strs)
        for index, stdname_str in enumerate(stdname_strs):
            if index == num_strs-1:
                suffix = ' /)'
            else:
                suffix = ', &'
            # end if
            outfile.write('{}{}'.format(stdname_str, suffix), indent+1)

        #Write a fourth blank space:
        outfile.write("", 0)

        #Write starting decleration of IC input name array:
        dec_string = \
            "character(len={}), public :: input_var_names({}, ic_var_num) = reshape((/ &".format(\
            ic_name_max_len, ic_name_max_num)
        outfile.write(dec_string, indent)

        #Write IC names to fortran array:
        num_strs = len(ic_name_strs)
        for index, ic_name_str in enumerate(ic_name_strs):
            if index == num_strs-1:
                suffix = ' /), (/{}, ic_var_num/))'.format(ic_name_max_num)
            else:
                suffix = ', &'
            # end if
            outfile.write('{}{}'.format(ic_name_str, suffix), indent+1)

        #Write a final blank space:
        outfile.write("", 0)

    @staticmethod
    def find_ic_name_max_len(variable_list):
        """Determine max length of input (IC) file variable names"""

        #Initialize max IC name string length variable:
        ic_name_max_len = 0

        #Loop over variables in list:
        for var in variable_list:
            #Check if variable actually has IC names:
            if var.ic_names is not None:
                #Loop over all IC input names for given variable:
                for ic_name in var.ic_names:
                    #Determine IC name string length:
                    ic_name_len = len(ic_name)

                    #Determine if standard name string length is longer
                    #then all prvious values:
                    if ic_name_len > ic_name_max_len:
                        #If so, then re-set max length variable:
                        ic_name_max_len = ic_name_len

        #Return max string length of input variable names:
        return ic_name_max_len

    @property
    def name(self):
        """Return this File's name"""
        return self.__name

    @property
    def file_type(self):
        """Return this File's type"""
        return self.__type

###############################################################################
def parse_command_line(args, description):
###############################################################################
    """Parse and return the command line arguments when
    this module is executed"""
    parser = argparse.ArgumentParser(description=description,
                                     formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument("registry_file",
                        metavar='<registry XML filename>',
                        type=str, help="XML file with CAM registry library")
    parser.add_argument("--dycore", type=str, required=True,
                        metavar='DYCORE (required)',
                        help="Dycore (EUL, FV, FV3, MPAS, SE, none)")
    parser.add_argument("--config", type=str, required=True,
                        metavar='CONFIG (required)',
                        help=("Comma-separated onfig items "
                              "(e.g., gravity_waves=True)"))
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Directory where output files will be written")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--debug", action='store_true',
                       help='Increase logging', default=False)
    group.add_argument("--quiet", action='store_true',
                       help='Disable logging except for errors', default=False)
    parser.add_argument("--indent", type=int, default=3,
                        help="Indent level for Fortran source code")
    pargs = parser.parse_args(args)
    return pargs

###############################################################################
def write_registry_files(registry, dycore, config, outdir, indent, logger):
###############################################################################
    """Write metadata and source files for <registry>

    >>> File(ET.fromstring('<variable name="physics_types" type="module"><user reference="kind_phys"/></variable>'), TypeRegistry(), 'eul', "", None) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Unknown registry object type, 'variable'
    """
    files = list()
    known_types = TypeRegistry()
    for section in registry:
        sec_name = section.get('name')
        logger.info("Parsing {}, {}, from registry".format(section.tag,
                                                           sec_name))
        if section.tag == 'file':
            files.append(File(section, known_types, dycore, config, logger))
        else:
            emsg = "Unknown registry object type, '{}'"
            raise CCPPError(emsg.format(section.tag))
        # end if
    # end for
    # Make sure output directory exists
    if not os.path.exists(outdir):
        os.makedirs(outdir)
    # end if
    # Write metadata
    for file_ in files:
        file_.write_metadata(outdir, logger)
        file_.write_source(outdir, indent, logger)
    # end for

###############################################################################
def gen_registry(registry_file, dycore, config, outdir, indent,
                 loglevel=None, logger=None, schema_paths=None,
                 error_on_no_validate=False):
###############################################################################
    """Parse a registry XML file and generate source code and metadata.
    <dycore> is the name of the dycore for DP coupling specialization.
    <config> is a dictionary containing other configuration items for
       souce code customization.
    Source code and metadata is output to <outdir>.
    <indent> is the number of spaces between indent levels.
    Set <debug> to True for more logging output."""
    if not logger:
        if not loglevel:
            loglevel = logging.INFO
        # end if
        logger = init_log(os.path.basename(__file__), loglevel)
    elif loglevel is not None:
        emsg = "gen_registry: Ignoring <loglevel> because logger is present"
        logger.debug(emsg)
    # end if
    if not schema_paths:
        schema_paths = [__CURRDIR]
    # end if
    logger.info("Reading CAM registry from %s", registry_file)
    _, registry = read_xml_file(registry_file)
    # Validate the XML file
    version = find_schema_version(registry)
    if 0 < logger.getEffectiveLevel() <= logging.DEBUG:
        verstr = '.'.join([str(x) for x in version])
        logger.debug("Found registry version, v%s", verstr)
    # end if
    schema_dir = None
    for spath in schema_paths:
        logger.debug("Looking for registry schema in '{}'".format(spath))
        schema_dir = find_schema_file("registry", version, schema_path=spath)
        if schema_dir:
            schema_dir = os.path.dirname(schema_dir)
            break
        # end if
    # end for
    try:
        emsg = "Invalid registry file, {}".format(registry_file)
        file_ok = validate_xml_file(registry_file, 'registry', version,
                                    logger, schema_path=schema_dir,
                                    error_on_noxmllint=error_on_no_validate)
    except CCPPError as ccpperr:
        cemsg = "{}".format(ccpperr).split('\n')[0]
        if cemsg[0:12] == 'Execution of':
            xstart = cemsg.find("'")
            if xstart >= 0:
                xend = cemsg[xstart + 1:].find("'") + xstart + 1
                emsg += '\n' + cemsg[xstart + 1:xend]
            # end if (else, just keep original message)
        elif cemsg[0:18] == 'validate_xml_file:':
            emsg += "\n" + cemsg
        # end if
        file_ok = False
    # end if
    if not file_ok:
        if error_on_no_validate:
            raise CCPPError(emsg)
        # end if
        logger.error(emsg)
        retcode = 1
    else:
        library_name = registry.get('name')
        emsg = "Parsing registry, {}".format(library_name)
        logger.debug(emsg)
        write_registry_files(registry, dycore, config, outdir, indent, logger)
        retcode = 0 # Throw exception on error
    # end if
    return retcode

def main():
    """Function to execute when module called as a script"""
    args = parse_command_line(sys.argv[1:], __doc__)
    if args.output_dir is None:
        outdir = os.getcwd()
    else:
        outdir = args.output_dir
    # end if
    if args.debug:
        loglevel = logging.DEBUG
    elif args.quiet:
        loglevel = logging.ERROR
    else:
        loglevel = logging.INFO
    # end if
    retcode = gen_registry(args.registry_file, args.dycore.lower(),
                           args.config, outdir, args.indent,
                           loglevel=loglevel)
    return retcode

###############################################################################
if __name__ == "__main__":
    __RETCODE = main()
    sys.exit(__RETCODE)
