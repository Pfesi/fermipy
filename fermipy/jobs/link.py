# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
Utilities to execute command line applications.

The main class is `Link`, which wraps a single command line application.

"""
from __future__ import absolute_import, division, print_function

import sys
import os
import copy
import argparse
import subprocess

from collections import OrderedDict

from fermipy.jobs.utils import is_null, is_not_null
from fermipy.jobs.file_archive import FileDict, FileStageManager
from fermipy.jobs.job_archive import get_timestamp, JobStatus, JobDetails, JOB_STATUS_STRINGS
from fermipy.jobs.factory import LinkFactory
from fermipy.jobs.sys_interface import SysInterface

def extract_arguments(args, defaults):
    """ Extract a set of arguments from a large dictionary

    Parameters
    ----------

    args : dict
        Dictionary with the arguments values to use

    defaults : dict
        Dictionary with all the argument to extract, and default values for each

    Returns dict filled with the arguments to pass to gtapp
    """
    out_dict = convert_option_dict_to_dict(defaults)
    for key in defaults.keys():
        mapped_val = args.get(key, None)
        if mapped_val is None:
            pass
        else:
            out_dict[key] = mapped_val
    return out_dict


def check_files(filelist,
                file_stage_manager=None,
                return_found=True,
                return_missing=True):
    """Check that all files in a list exist

    Return two lists: (found, missing)
    """
    found = []
    missing = []
    none_count = 0
    for fname in filelist:

        if fname is None:
            none_count += 1
            continue
        if fname[0] == '@':
            fname = fname[1:]
        if os.path.exists(fname):
            found.append(fname)
            continue
        if os.path.exists(fname + '.gz'):
            found.append(fname)
            continue
        if file_stage_manager is not None:
            fname = file_stage_manager.get_scratch_path(fname)
            if os.path.exists(fname):
                found.append(fname)
                continue
        missing.append(fname)
    if return_found and return_missing:
        return found, missing
    elif return_found:
        return found
    elif return_missing:
        return missing
    else:
        return None


def add_argument(parser, dest, info):
    """ Add an argument to an `argparse.ArgumentParser` object """
    default, helpstr, typeinfo = info

    if dest=='args':
        parser.add_argument('args', nargs='+', default=None, help=helpstr)
    elif typeinfo == list:
        parser.add_argument('--%s' % dest, action='append', help=helpstr)
    elif typeinfo == bool:
        parser.add_argument('--%s' % dest, action='store_true', help=helpstr)
    else:
        parser.add_argument('--%s' % dest, action='store', type=typeinfo,
                            default=default, help=helpstr)


def convert_value_to_option_tuple(value, helpstr=None):
    """Convert a value to a tuple of the form expected by `Link.options`

    Returns (value, helpstr, type(value)
    """
    if helpstr is None:
        helpstr = "Unknown"
    return (value, helpstr, type(value))


def convert_dict_to_option_dict(input_dict):
    """Convert a simple key-value dictionary to a dictionary of options tuples"""
    ret_dict = {}
    for key, value in input_dict.items():
        ret_dict[key] = convert_value_to_option_tuple(value)
    return ret_dict


def convert_option_dict_to_dict(option_dict):
    """Convert a dictionary of options tuples to a simple key-value dictionary"""
    ret_dict = {}
    for key, value in option_dict.items():
        if is_null(value):
            ret_dict[key] = None
        elif isinstance(value, tuple):
            ret_dict[key] = value[0]
        else:
            ret_dict[key] = value
    return ret_dict


class Link(object):
    """A wrapper for a command line application.

    This class keeps track for the arguments to pass to the application
    as well as input and output files.

    This can be used either with other Link to build a chain, or as
    as standalone wrapper to pass conifguration to the application.

    Parameters
    ----------

    appname : str
        Name of the application
    args : dict
        Up-to-date dictionary with the arguments that will be passed to the application
    options : dict
        Dictionary with the options that we are allowed to set and default values
    files : `FileDict`
        Object that keeps track of input and output files
    jobs : `OrderedDict`
        Dictionary mapping keys to `JobDetails`
    """
    topkey = '__top__'

    def __init__(self, linkname, **kwargs):
        """ C'tor

        Parameters
        -----------
        linkname : str
            Unique name of this particular link

        Keyword arguments
        -----------
        appname : str
            Name of the application (e.g., gtbin)
        parser: `argparse.ArguemntParser'
            Parser with the options that we are allow to set and default values
        options : dict
            Dictionary with the tuples defining that we are allowed to set and default values
        file_args : dict
            Dictionary mapping argument to `FileFlags' enum
        file_stage : `FileStageManager`
            Manager for staging files to and from a scratch area
        """
        self.linkname = linkname
        self.link_prefix = kwargs.get('link_prefix', '')
        self._parser = kwargs.pop('parser', None)
        self._interface = kwargs.pop('interface', SysInterface())
        self._options = {}
        self._options.update(kwargs.pop('options', {}))
        if self._parser is not None:
            self._fill_argparser(self._parser)
        self._file_stage = kwargs.get('file_stage', None)
        self._job_archive = kwargs.get('job_archive', None)
        self.args = {}
        self.args.update(convert_option_dict_to_dict(self._options))
        self.files = FileDict(**kwargs)
        self.sub_files = FileDict()
        self.jobs = OrderedDict()

    @classmethod
    def _init_dict(cls, **kwargs):
        """ Build a dictionary to pass to the __init__ function """
        linkname = kwargs.pop('linkname', cls.linkname_default)
        parser = argparse.ArgumentParser(usage=cls.usage, description=cls.description)
        if hasattr(cls, 'default_file_args'):
            file_args=cls.default_file_args.copy()
        else:
            file_args={}
        o = dict(parser=parser,
                 options=cls.default_options.copy(),
                 file_args=file_args,
                 **kwargs)
        return linkname, o

    @classmethod
    def create(cls, **kwargs):
        """ Build and return a `Link` """
        return cls(**kwargs)

    @classmethod
    def main(cls):
        """ Hook to run this `Link` from the command line """
        link = cls.create()
        link.run_analysis(sys.argv[1:])

    @classmethod
    def register_class(cls):
        from fermipy.jobs.factory import LinkFactory   
        if LinkFactory._class_dict.has_key(cls.appname):
            return
        LinkFactory.register(cls.appname, cls)
        if hasattr(cls, '_register_links'):
            cls._register_links()
        

    @property
    def arg_names(self):
        """Return the list of arg names """
        return [self.args.keys()]

    @property
    def full_linkname(self):
        return self.link_prefix+self.linkname

    def _get_args(self):
        """Internal function to cast self._options into dictionary

        Returns dict with argument key : value pairings
        """
        args = {}
        args.update(convert_option_dict_to_dict(self._options))
        return args

    def _make_argv(self):
        """Generate the vector of arguments for this `Link`.

        This is exactly the 'argv' generated for the
        command as called from the Unix command line.
        """
        command = self.formatted_command()
        tokens = command.split()
        return tokens[1:]

    def _fill_argparser(self, parser):
        """Fill an `argparser.ArgumentParser` with the options from this chain
        """
        for key, val in self._options.items():
            add_argument(parser, key, val)

    def _latch_file_info(self):
        """Internal function to update the dictionaries
        keeping track of input and output files
        """
        self.files.file_dict.clear()
        self.files.latch_file_info(self.args)

    def _update_options(self, input_dict):
        """Update the values in self.options

        Parameters
        ----------
        input_dict : dict
            Dictionary with argument key : value pairings

        Inserts values into self._options
        """
        for key, value in input_dict.items():
            new_tuple = (value, self._options[key][1], self._options[key][2])
            self._options[key] = new_tuple

    def _update_sub_file_dict(self, sub_files):
        """Update a file dict with information from self"""
        sub_files.file_dict.clear()
        for job_details in self.jobs.values():
            if job_details.file_dict is not None:
                sub_files.update(job_details.file_dict)
            if job_details.sub_file_dict is not None:
                sub_files.update(job_details.sub_file_dict)
                
    def _pre_run_checks(self, stream=sys.stdout, dry_run=False):
        """Do some checks before running this link

        This checks if input and output files are present.

        If input files are missing this will raise `OSError` if dry_run is False
        If all output files are present this return False.

        Parameters
        -----------
        stream : `file`
            Must have 'write' function

        dry_run : bool
            Print command but do not run it

        Returns bool
            True if it is ok to proceed with running the link
        """
        input_missing = self.check_input_files(return_found=False)
        if len(input_missing) != 0:
            if dry_run:
                stream.write("Input files are missing: %s: %i\n" %
                             (self.linkname, len(input_missing)))
            else:
                raise OSError("Input files are missing: %s" % input_missing)

        output_found, output_missing = self.check_output_files()
        if len(output_missing) == 0 and len(output_found) > 0:
            stream.write("All output files for %s already exist: %i %i %i\n" %
                         (self.linkname, len(output_found),
                          len(output_missing), len(self.files.output_files)))
            if dry_run:
                pass
            else:
                pass
                #return False
        return True

    def _set_file_stage(self, file_stage):
        """Set this link to use a `FileStageManager` to copy files
        to and from a scratch area
        """
        self._file_stage = file_stage

    def _create_job_details(self, key, job_config, logfile, status):
        """Create a `JobDetails` for a single job

        Parameters
        ----------

        key : str
            Key used to identify this particular job

        job_config : dict
            Dictionary with arguements passed to this particular job

        logfile : str
            Name of the associated log file

        status : int
            Current status of the job

        Returns `JobDetails`
        """
        #try:
        self.update_args(job_config)
        #except IOError:
            # This means that some input file need for the 
            # configuraiton hasn't been created yet
        #sys.stderr.write("Warning %s could not be configured\n"%self.full_linkname)
        job_details = JobDetails(jobname=self.full_linkname,
                                 jobkey=key,
                                 appname=self.appname,
                                 logfile=logfile,
                                 job_config=job_config,
                                 timestamp=get_timestamp(),
                                 file_dict=copy.deepcopy(self.files),
                                 sub_file_dict=copy.deepcopy(self.sub_files),
                                 status=status)
        return job_details


    def _map_scratch_files(self, file_dict):
        """Build and return the mapping for copying files to and from scratch area"""
        if self._file_stage is None:
            return ({}, {})
        input_files = file_dict.input_files_to_stage
        output_files = file_dict.output_files_to_stage
        input_file_mapping = self._file_stage.map_files(input_files)
        output_file_mapping = self._file_stage.map_files(output_files)
        self._update_file_args(input_file_mapping)
        self._update_file_args(output_file_mapping)
        return input_file_mapping, output_file_mapping

    def _update_file_args(self, file_mapping):
        """Adjust the arguments to deal with staging files to the scratch area"""
        for key, value in self.args.items():
            new_value = file_mapping.get(value, value)
            if new_value != value:
                self.args[key] = new_value

    def _stage_input_files(self, file_mapping, dry_run=True):
        """Stage the input files to the scratch area and adjust the arguments accordingly"""
        #print ("Staging input ", file_mapping)
        if self._file_stage is None:
            return
        self._file_stage.copy_to_scratch(file_mapping, dry_run)

    def _stage_output_files(self, file_mapping, dry_run=True):
        """Stage the input files to the scratch area and adjust the arguments accordingly"""
        #print ("Staging output ", file_mapping)
        if self._file_stage is None:
            return
        self._file_stage.copy_from_scratch(file_mapping, dry_run)

    def _run_link(self, stream=sys.stdout, dry_run=False, stage_files=True, resubmit_failed=False):
        """Runs this link.

        This checks if input and output files are present.

        If input files are missing this will raise `OSError` if dry_run is False
        If all output files are present this will skip execution.

        Parameters
        -----------
        stream : `file`
            Must have 'write' function

        dry_run : bool
            Print command but do not run it

        stage_files : bool
            Stage files to and from the scratch area
        """
        check_ok = self._pre_run_checks(stream, dry_run)
        if not check_ok:
            return

        if self._file_stage is not None:
            input_file_mapping, output_file_mapping = self._map_scratch_files(
                self.files)
            if stage_files:
                self._file_stage.make_scratch_dirs(input_file_mapping, dry_run)
                self._file_stage.make_scratch_dirs(
                    output_file_mapping, dry_run)
                self._stage_input_files(input_file_mapping, dry_run)

        return_code = self.run_command(stream, dry_run)
        print ("return code ", return_code)
        if return_code == 0:
            status = JobStatus.done
            if self._file_stage is not None and stage_files:
                self._stage_output_files(output_file_mapping, dry_run)
            self._finalize(dry_run)
        else:
            status = JobStatus.failed
        if dry_run:
            return
        self._write_status_to_log(return_code, stream)
        self._set_status_self(status=status)

    def _register_job(self, key, job_config, logfile, status):
        """Create a `JobDetails` for this link
        and add it to the self.jobs dictionary.

        Parameters
        ----------

        key : str
            Key used to identify this particular job

        job_config : dict
            Dictionary with arguments passed to this particular job

        logfile : str
            Name of the associated log file

        status : int
            Current status of the job

        Returns `JobDetails`
        """
        job_details = self._create_job_details(key, job_config, logfile, status)
        self.jobs[job_details.fullkey] = job_details
        return job_details

    def _register_self(self, logfile, key=JobDetails.topkey, status=JobStatus.unknown):
        """Runs this link, captures output to logfile, 
        and records the job in self.jobs"""
        fullkey = JobDetails.make_fullkey(self.full_linkname, key)        
        if self.jobs.has_key(fullkey):
            job_details = self.jobs[fullkey]
            job_details.status = status
        else:
            job_details = self._register_job(key, self.args, logfile, status)

    def _archive_self(self, logfile, key=JobDetails.topkey, status=JobStatus.unknown):
        """Write this link to the job archive"""
        self._register_self(logfile, key, status)
        if self._job_archive is None:
            return
        self._job_archive.register_jobs(self.get_jobs())

    def _set_status_self(self, key=JobDetails.topkey, status=JobStatus.unknown):
        """ Set the status of this job """
        fullkey = JobDetails.make_fullkey(self.full_linkname, key)                
        if self.jobs.has_key(fullkey):
            self.jobs[fullkey].status = status          
            if self._job_archive:
                self._job_archive.register_job(self.jobs[fullkey])
        else:
            self._register_self('dummy.log', key, status)

    def _write_status_to_log(self, return_code, stream=sys.stdout):
        """ Write the status of this job to a log stream """
        stream.write("Timestamp: %i\n"%get_timestamp())
        if return_code == 0:
            stream.write("%s\n"%self._interface.string_successful)
        else:
            stream.write("%s %i\n"%(self._interface.string_exited, return_code))
 
    def _finalize(self, dry_run=False):
        """Remove / compress files as requested """
        for rmfile in self.files.temp_files:
            if dry_run:
                print("remove %s" % rmfile)
            else:
                os.remove(rmfile)
        for gzfile in self.files.gzip_files:
            if dry_run:
                #print ("gzip %s" % gzfile)
                pass
            else:
                os.system('gzip -9 %s' % gzfile)



    def update_args(self, override_args):
        """Update the argument used to invoke the application

        Note that this will also update the dictionary of input and output files.

        Parameters
        -----------
        override_args : dict
            Dictionary of arguments to override the current values
        """
        self.args = extract_arguments(override_args, self.args)
        self._latch_file_info()
        scratch_dir = self.args.get('scratch', None)
        if is_not_null(scratch_dir):
            self._file_stage = FileStageManager(scratch_dir, '.')

    def get_failed_jobs(self, fail_running=False, fail_pending=False):
        """Return a dictionary with the subset of jobs that are marked as failed"""
        failed_jobs = {}
        for job_key, job_details in self.jobs.items():
            if job_details.status == JobStatus.failed:
                failed_jobs[job_key] = job_details
            elif job_details.status == JobStatus.partial_failed:
                failed_jobs[job_key] = job_details
            elif fail_running and job_details.status == JobStatus.running:
                failed_jobs[job_key] = job_details
            elif fail_pending and job_details.status <= JobStatus.pending:
                failed_jobs[job_key] = job_details
        return failed_jobs

    def check_job_status(self, key=JobDetails.topkey,                          
                         fail_running=False,
                         fail_pending=False,
                         force_check=False):
        """Check the status of a particular job
        
        By default this checks the status of the top-level job
        """
        if self.jobs.has_key(key):
            status = self.jobs[key].status
            if status in [JobStatus.unknown, JobStatus.ready, 
                          JobStatus.pending, JobStatus.running] or force_check:
                status = self._interface.check_job(self.jobs[key])            
            if status == JobStatus.running and fail_running:
                status = JobStatus.failed
            if status == JobStatus.pending and fail_pending:
                status = JobStatus.failed
            self.jobs[key].status = status
            if self._job_archive:
                self._job_archive.register_job(self.jobs[key])
        else:
            status = JobStatus.no_job
        
        return status

    def check_jobs_status(self, 
                          fail_running=False,
                          fail_pending=False):
        """Check the status of all the jobs run from this link """
        n_failed = 0
        n_passed = 0
        n_total = 0
        for job_key, job_details in self.jobs.items():
            n_total +=1
            if job_details.status == JobStatus.failed:
                n_failed += 1
            elif job_details.status == JobStatus.partial_failed:
                n_failed += 1
            elif fail_running and job_details.status == JobStatus.running:
                n_failed += 1
            elif fail_pending and job_details.status == JobStatus.pending:
                n_failed += 1
            elif job_details.status == JobStatus.done:
                n_passed +=1

        if n_failed > 0:
            if n_passed > 0:
                return JobStatus.partial_failed
            else:
                return JobStatus.failed
        elif n_passed == n_total:
            return JobStatus.done
        elif n_passed > 0:
            return JobStatus.running

        return JobStatus.pending
       
    def clear_jobs(self, recursive=True):
        """Clear the dictionary with all the jobs

        If recursive is True this will include jobs from internal `Link`
        """
        self.jobs.clear()

    def clean_jobs(self, clean_all=False):
        """ """ 
        self._interface.clean_jobs(self,
                                   job_archive=self._job_archive)

    def get_jobs(self, recursive=True):
        """Return a dictionary with all the jobs

        If recursive is True this will include jobs from internal `Link`
        """                
        if recursive:
            ret_dict = self.jobs.copy()
            return ret_dict
        else:
            return self.jobs

    def check_input_files(self,
                          return_found=True,
                          return_missing=True):
        """Check if input files exist.

        Return two lists: (found, missing)
        """
        all_input_files = self.files.chain_input_files + self.sub_files.chain_input_files
        return check_files(all_input_files, self._file_stage,
                           return_found, return_missing)

    def check_output_files(self,
                           return_found=True,
                           return_missing=True):
        """Check if output files exist.

        Return two lists: (found, missing)
        """
        all_output_files = self.files.chain_output_files + self.sub_files.chain_output_files
        return check_files(all_output_files, self._file_stage,
                           return_found, return_missing)

    def missing_input_files(self):
        """Make and return a dictionary of the missing input files.

        This returns a dictionary mapping
        filepath to list of links that use the file as input.
        """
        missing = self.check_input_files(return_found=False)
        ret_dict = {}
        for miss_file in missing:
            ret_dict[miss_file] = [self.linkname]
        return ret_dict

    def missing_output_files(self):
        """Make and return a dictionary of the missing output files.

        This returns a dictionary mapping
        filepath to list of links that product the file as output.
        """
        missing = self.check_output_files(return_found=False)
        ret_dict = {}
        for miss_file in missing:
            ret_dict[miss_file] = [self.linkname]
        return ret_dict

    def formatted_command(self):
        """Build and return the formatted command for this `Link`.

        This is exactly the command as called from the Unix command line.
        """
        command_template = self.command_template()
        format_dict = self.args.copy()
        
        for key, value in format_dict.items():
            # protect whitespace
            if isinstance(value, list):
                outstr = ""
                if key == 'args':
                    outkey = ""
                else:
                    outkey = "--%s "
                for lval in value:
                    outstr += ' '
                    outstr += outkey
                    outstr += lval
                format_dict[key] = '"%s"'%outstr
            elif isinstance(value, str) and value.find(' ') >= 0 and key != 'args':
                format_dict[key] = '"%s"'%value

        command = command_template.format(**format_dict)
        return command
 

    def run_command(self, stream=sys.stdout, dry_run=False):
        """Runs the command for this link.  This method can be overridden by
        sub-classes to invoke a different command

        Parameters
        -----------
        stream : `file`
            Must have 'write' function

        dry_run : bool
            Print command but do not run it
        """
        command = self.formatted_command()
        if dry_run:
            stream.write("%s\n" % command)
            stream.flush()
            return 0
        else:
            p = subprocess.Popen(command.split(), 
                                 stderr=stream,
                                 stdout=stream)
            p.communicate()
            return p.returncode


    def run(self, stream=sys.stdout, dry_run=False, stage_files=True, resubmit_failed=False):
        """Runs this link.

        This version is intended to be overwritten by sub-classes so
        as to provide a single function that behaves the same
        for all version of `Link`

        Parameters
        -----------
        stream : `file`
            Must have 'write' function

        dry_run : bool
            Print command but do not run it
        """
        self._run_link(stream, dry_run, stage_files, resubmit_failed)

    def run_with_log(self, dry_run=False, stage_files=True, resubmit_failed=False):
        """Runs this link with output sent to a pre-defined logfile

        Parameters
        -----------
        dry_run : bool
            Print command but do not run it
        """
        fullkey = JobDetails.make_fullkey(self.full_linkname)
        job_details = self.jobs[fullkey]
        odir = os.path.dirname(job_details.logfile)
        try:
            os.makedirs(odir)
        except OSError:
            pass
        ostream = open(job_details.logfile, 'w')
        self.run(ostream, dry_run, stage_files, resubmit_failed)


    def command_template(self):
        """Build and return a string that can be used as a template invoking
        this chain from the command line.

        The actual command can be obtainted by using
        `self.command_template().format(**self.args)`
        """
        com_out = self.appname
        arg_string = ""
        flag_string = ""
        # Loop over the key, value pairs in self.args

        for key, val in self.args.items():
            # Check if the value is set in self._options
            # If so, get the value from there
            if val is None:
                opt_val = self._options[key][0]
            else:
                opt_val = val
            opt_type = self._options[key][2]
            if key == 'args':
                # 'args' is special, pull it out and move it to the back                
                arg_string += ' {%s}' % key
            elif opt_type is bool:
                if opt_val:
                    flag_string += ' --%s' % (key)
            elif opt_type is list:
                if is_null(opt_val):
                    continue
                elif isinstance(opt_val, str):
                    com_out += ' --%s %s' % (key, opt_val)
                elif isinstance(opt_val, list):
                    for arg_val in opt_val:
                        com_out += ' --%s %s' % (key, arg_val)
            else:
                com_out += ' --%s {%s}' % (key, key)
        com_out += flag_string
        com_out += arg_string
        return com_out

    def run_argparser(self, argv):
        """Initialize a link with a set of arguments using an `argparser.ArgumentParser`
        """
        if self._parser is None:
            raise ValueError('Link was not given a parser on initialization')
        args = self._parser.parse_args(argv)
        self.update_args(args.__dict__)
        return args

    def print_summary(self, stream=sys.stdout, indent="", recurse_level=2):
        """Print a summary of the activity done by this `Link`.

        Parameters
        -----------
        stream : `file`
            Stream to print to
        indent : str
            Indentation at start of line
        recurse_level : int
            Number of recursion levels to print
        """
        if recurse_level < 0:
            return
        stream.write("%sLink: %s\n" % (indent, self.linkname))
        stream.write("%sN_jobs: %s\n" % (indent, len(self.get_jobs())))
        self.sub_files.print_chain_summary(stream, indent)
