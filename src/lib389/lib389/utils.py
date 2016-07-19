# --- BEGIN COPYRIGHT BLOCK ---
# Copyright (C) 2015 Red Hat, Inc.
# All rights reserved.
#
# License: GPL (version 3 or any later version).
# See LICENSE for details.
# --- END COPYRIGHT BLOCK ---

"""Utilities for DirSrv.

    TODO put them in a module!
"""
try:
    from subprocess import Popen as my_popen, PIPE
except ImportError:
    from popen2 import popen2

    def my_popen(cmd_l, stdout=None):
        class MockPopenResult(object):
            def wait(self):
                pass
        p = MockPopenResult()
        p.stdout, p.stdin = popen2(cmd_l)
        return p

import re
import os
import logging
import shutil
import ldap
import socket
import subprocess
import time
import sys
import filecmp
from socket import getfqdn
from ldapurl import LDAPUrl
from contextlib import closing

from lib389._constants import *
from lib389.properties import *

MAJOR, MINOR, _, _, _ = sys.version_info

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)
#
# Decorator
#


def static_var(varname, value):
    def decorate(func):
        setattr(func, varname, value)
        return func
    return decorate


#
# Various searches to be used in getEntry
#   eg getEntry(*searches['NAMINGCONTEXTS'])
#
searches = {
    'NAMINGCONTEXTS': ('', ldap.SCOPE_BASE, '(objectclass=*)',
                       ['namingcontexts']),
    'ZOMBIE': ('', ldap.SCOPE_SUBTREE,
               '(&(objectclass=glue)(objectclass=extensibleobject))', ['dn'])
}

#
# Utilities
#


def is_a_dn(dn, allow_anon=True):
    """Returns True if the given string is a DN, False otherwise."""
    try:
        if dn == "" and allow_anon is True:
            # The empty string is anonymous.
            return True
        if len(ldap.dn.str2dn(dn)) > 0:
            # We have valid components in the dn.
            return True
    except ldap.DECODING_ERROR:
        # An invalid dn was given.
        pass
    except TypeError:
        # An invalid type was passed to be checked
        pass
    return False


def normalizeDN(dn, usespace=False):
    # not great, but will do until we use a newer version of python-ldap
    # that has DN utilities
    ary = ldap.explode_dn(dn.lower())
    joinstr = ","
    if usespace:
        joinstr = ", "
    return joinstr.join(ary)


def escapeDNValue(dn):
    '''convert special characters in a DN into LDAPv3 escapes.

     e.g.
    "dc=example,dc=com" -> \"dc\=example\,\ dc\=com\"'''
    for cc in (' ', '"', '+', ',', ';', '<', '>', '='):
        dn = dn.replace(cc, '\\' + cc)
    return dn


def escapeDNFiltValue(dn):
    '''convert special characters in a DN into LDAPv3 escapes
    for use in search filters'''
    for cc in (' ', '"', '+', ',', ';', '<', '>', '='):
        dn = dn.replace(cc, '\\%x' % ord(cc))
    return dn


def suffixfilt(suffix):
    """Return a filter matching any possible suffix form.

        eg. normalized, escaped, spaced...
    """
    nsuffix = normalizeDN(suffix)
    spacesuffix = normalizeDN(nsuffix, True)
    escapesuffix = escapeDNFiltValue(nsuffix)
    filt = ('(|(cn=%s)(cn=%s)(cn=%s)(cn="%s")(cn="%s")(cn=%s)(cn="%s"))' %
            (escapesuffix, nsuffix, spacesuffix, nsuffix, spacesuffix, suffix,
             suffix))
    return filt


#
# path tools
#
def get_sbin_dir(sroot=None, prefix=None):
    """Return the sbin directory (default /usr/sbin)."""
    if sroot:
        return "%s/bin/slapd/admin/bin" % sroot
    elif prefix and prefix != '/':
        return "%s/sbin" % prefix
    return "/usr/sbin"


def get_bin_dir(sroot=None, prefix=None):
    """Return the sbin directory (default /usr/bin)."""
    if sroot:
        return "%s/bin/slapd/admin/bin" % sroot
    elif prefix and prefix != '/':
        return "%s/bin" % prefix
    return "/usr/bin"


def get_data_dir(prefix=None):
    """Return the shared data directory (default /usr/share/dirsrv/data)."""
    if prefix and prefix != '/':
        return "%s/share/dirsrv/data" % prefix
    return "/usr/share/dirsrv/data"


def get_plugin_dir(prefix=None):
    """
    Return the plugin directory (default /usr/lib64/dirsrv/plugins).
    This should be 64/32bit aware.
    """
    if prefix and prefix != '/':
        # With prefix installations, even 64 bit, it can be under /usr/lib/
        if os.path.exists("%s/usr/lib/dirsrv/plugins" % prefix):
            return "%s/usr/lib/dirsrv/plugins" % prefix
        else:
            if os.path.exists("%s/usr/lib64/dirsrv/plugins" % prefix):
                return "%s/usr/lib64/dirsrv/plugins" % prefix

    # Need to check for 32/64 bit installations
    if not os.path.exists("/usr/lib64/dirsrv/plugins"):
        if os.path.exists("/usr/lib/dirsrv/plugins"):
            return "/usr/lib/dirsrv/plugins"
    return "/usr/lib64/dirsrv/plugins"


#
# valgrind functions
#
def valgrind_enable(sbin_dir, wrapper=None):
    '''
    Copy the valgrind ns-slapd wrapper into the /sbin directory
    (making a backup of the original ns-slapd binary).

    The script calling valgrind_enable() must be run as the 'root' user
    as selinux needs to be disabled for valgrind to work

    The server instance(s) should be stopped prior to calling this function.
    Then after calling valgrind_enable():
    - Start the server instance(s) with a timeout of 60 (valgrind takes a
      while to startup)
    - Run the tests
    - Stop the server
    - Get the results file
    - Run valgrind_check_file(result_file, "pattern", "pattern", ...)
    - Run valgrind_disable()

    :param sbin_dir: the location of the ns-slapd binary (e.g. /usr/sbin)
    :param wrapper: The valgrind wrapper script for ns-slapd (if not set,
                     a default wrapper is used)
    :raise IOError: If there is a problem setting up the valgrind scripts
    :raise EnvironmentError: If script is not run as 'root'
    '''

    if os.geteuid() != 0:
        log.error('This script must be run as root to use valgrind')
        raise EnvironmentError

    if not wrapper:
        # use the default ns-slapd wrapper
        wrapper = '%s/%s' % (os.path.dirname(os.path.abspath(__file__)),
                             VALGRIND_WRAPPER)

    nsslapd_orig = '%s/ns-slapd' % sbin_dir
    nsslapd_backup = '%s/ns-slapd.original' % sbin_dir

    if os.path.isfile(nsslapd_backup):
        # There is a backup which means we never cleaned up from a previous
        # run(failed test?)
        if not filecmp.cmp(nsslapd_backup, nsslapd_orig):
            # Files are different sizes, we assume valgrind is already setup
            log.info('Valgrind is already enabled.')
            return

    # Check both nsslapd's exist
    if not os.path.isfile(wrapper):
        raise IOError('The valgrind wrapper (%s) does not exist. file=%s' %
                      (wrapper, __file__))

    if not os.path.isfile(nsslapd_orig):
        raise IOError('The binary (%s) does not exist or is not accessible.' %
                      nsslapd_orig)

    # Make a backup of the original ns-slapd and copy the wrapper into place
    try:
        shutil.copy2(nsslapd_orig, nsslapd_backup)
    except IOError as e:
        log.fatal('valgrind_enable(): failed to backup ns-slapd, error: %s' %
                  e.strerror)
        raise IOError('failed to backup ns-slapd, error: %s' % e.strerror)

    # Copy the valgrind wrapper into place
    try:
        shutil.copy2(wrapper, nsslapd_orig)
    except IOError as e:
        log.fatal('valgrind_enable(): failed to copy valgrind wrapper '
                  'to ns-slapd, error: %s' % e.strerror)
        raise IOError('failed to copy valgrind wrapper to ns-slapd, error: %s' %
                      e.strerror)

    # Disable selinux
    os.system('setenforce 0')

    log.info('Valgrind is now enabled.')


def valgrind_disable(sbin_dir):
    '''
    Restore the ns-slapd binary to its original state - the server instances
    are expected to be stopped.

    Note - selinux is enabled at the end of this process.

    :param sbin_dir - the location of the ns-slapd binary (e.g. /usr/sbin)
    :raise ValueError
    :raise EnvironmentError: If script is not run as 'root'
    '''

    if os.geteuid() != 0:
        log.error('This script must be run as root to use valgrind')
        raise EnvironmentError

    nsslapd_orig = '%s/ns-slapd' % sbin_dir
    nsslapd_backup = '%s/ns-slapd.original' % sbin_dir

    # Restore the original ns-slapd
    try:
        shutil.copyfile(nsslapd_backup, nsslapd_orig)
    except IOError as e:
        log.fatal('valgrind_disable: failed to restore ns-slapd, error: %s' %
                  e.strerror)
        raise ValueError('failed to restore ns-slapd, error: %s' % e.strerror)

    # Delete the backup now
    try:
        os.remove(nsslapd_backup)
    except OSError as e:
        log.fatal('valgrind_disable: failed to delete backup ns-slapd, error:'
                  ' %s' % e.strerror)
        raise ValueError('Failed to delete backup ns-slapd, error: %s' %
                         e.strerror)

    # Enable selinux
    os.system('setenforce 1')

    log.info('Valgrind is now disabled.')


def valgrind_get_results_file(dirsrv_inst):
    """
    Return the valgrind results file for the dirsrv instance.
    """

    """
    The "ps -ef | grep valgrind" looks like:

        nobody 26239 1 10 14:33 ? 00:00:06 valgrind -q --tool=memcheck
        --leak-check=yes --leak-resolution=high --num-callers=50
        --log-file=/var/tmp/slapd.vg.26179 /usr/sbin/ns-slapd.original
        -D /etc/dirsrv/slapd-localhost -i /var/run/dirsrv/slapd-localhost.pid
        -w /var/run/dirsrv/slapd-localhost.startpid

    We need to extract the "--log-file" value
    """
    cmd = ("ps -ef | grep valgrind | grep 'slapd-" + dirsrv_inst.serverid +
           " ' | awk '{ print $14 }' | sed -e 's/\-\-log\-file=//'")

    # Run the command and grab the output
    p = os.popen(cmd)
    results_file = p.readline()
    p.close()

    return results_file


def valgrind_check_file(results_file, *patterns):
    '''
    Check the valgrind results file for the all the patterns
    @param result_file - valgrind results file (must be read after server is
                         stopped)
    @param patterns - A plain text or regex pattern string args that should
                      be searched for
    @return True/False - Return true if one if the patterns match a stack
                         trace
    @raise IOError
    '''

    # Verify results file
    if not results_file:
        assert False

    # Check the result file fo the leak text
    results_file = results_file.replace('\n', '')
    found = False
    pattern_count = len(patterns)
    matched_count = 0

    vlog = open(results_file)
    for line in vlog:
        for match_txt in patterns:
            if re.search(match_txt, line):
                matched_count += 1
                break

        if len(line.split()) == 1:
            # Check if this stack stack matched all the patterns
            if pattern_count == matched_count:
                found = True
                print('valgrind: match found in results file: %s' %
                      (results_file))
                break
            else:
                matched_count = 0
    vlog.close()

    return found


#
# functions using sockets
#
def isLocalHost(host_name):
    """True if host_name points to a local ip.

        Uses gethostbyname()
    """
    # first see if this is a "well known" local hostname
    if host_name == 'localhost' or \
       host_name == 'localhost.localdomain' or \
       host_name == socket.gethostname():
        return True

    # first lookup ip addr
    try:
        ip_addr = socket.gethostbyname(host_name)
        if ip_addr.startswith("127."):
            # log.debug("this ip is on loopback, retain only the first octet")
            ip_addr = '127.'
    except socket.gaierror:
        log.debug("no ip address for %r" % host_name)
        return False

    # next, see if this IP addr is one of our
    # local addresses
    p = my_popen(['/sbin/ip', 'addr'], stdout=PIPE)
    child_stdout = p.stdout.read()
    found = ('inet %s' % ip_addr).encode() in child_stdout
    p.wait()

    return found


def getdomainname(name=''):
    fqdn = getfqdn(name)
    index = fqdn.find('.')
    if index >= 0:
        return fqdn[index + 1:]
    else:
        # This isn't correct. There is no domain name, so return empty.str
        # return fqdn
        return ""


def getdefaultsuffix(name=''):
    dm = getdomainname(name)
    if dm:
        return "dc=" + dm.replace('.', ',dc=')
    else:
        return 'dc=localdomain'


def get_server_user(args):
    """Return the unix username used from the server inspecting the following
       keys in args.

        'newuserid', 'admconf', 'sroot' -> ssusers.conf

    """
    if 'newuserid' not in args:
        if 'admconf' in args:
            args['newuserid'] = args['admconf'].SuiteSpotUserID
        elif 'sroot' in args:
            ssusers = open("%s/shared/config/ssusers.conf" % args['sroot'])
            for line in ssusers:
                ary = line.split()
                if len(ary) > 1 and ary[0] == 'SuiteSpotUser':
                    args['newuserid'] = ary[-1]
            ssusers.close()
    if 'newuserid' not in args:
        args['newuserid'] = os.environ['LOGNAME']
        if args['newuserid'] == 'root':
            args['newuserid'] = DEFAULT_USER


def update_newhost_with_fqdn(args):
    """Replace args[SER_HOST] with its fqdn and returns True if local.

    One of the arguments to createInstance is newhost.  If this is specified,
    we need to convert it to the fqdn.  If not given, we need to figure out
    what the fqdn of the local host is.  This method sets newhost in args to
    the appropriate value and returns True if newhost is the localhost, False
    otherwise"""
    if SER_HOST in args:
        args[SER_HOST] = getfqdn(args[SER_HOST])
        isLocal = isLocalHost(args[SER_HOST])
    else:
        isLocal = True
        args[SER_HOST] = getfqdn()
    return isLocal


def getcfgdsuserdn(cfgdn, args):
    """Return a DirSrv object bound anonymously or to the admin user.

    If the config ds user ID was given, not the full DN, we need to figure
    out the full DN.

    Try in order to:
        1- search the directory anonymously;
        2- look in ldap.conf;
        3- try the default DN.

    This may raise a file or LDAP exception.
    """
    # create a connection to the cfg ds
    conn = lib389.DirSrv(args['cfgdshost'], args['cfgdsport'], "", "", None)
    # if the caller gave a password, but not the cfguser DN, look it up
    if 'cfgdspwd' in args and \
            ('cfgdsuser' not in args or not is_a_dn(args['cfgdsuser'])):
        if 'cfgdsuser' in args:
            ent = conn.getEntry(cfgdn, ldap.SCOPE_SUBTREE,
                                "(uid=%s)" % args['cfgdsuser'],
                                ['dn'])
            args['cfgdsuser'] = ent.dn
        elif 'sroot' in args:
            ldapconf = open(
                "%s/shared/config/ldap.conf" % args['sroot'], 'r')
            for line in ldapconf:
                ary = line.split()  # default split is all whitespace
                if len(ary) > 1 and ary[0] == 'admnm':
                    args['cfgdsuser'] = ary[-1]
            ldapconf.close()
        elif 'admconf' in args:
            args['cfgdsuser'] = args['admconf'].userdn
        elif 'cfgdsuser' in args:
            dsuser = ("uid=%s,ou=Administrators,ou=TopologyManagement,%s" %
                      (args['cfgdsuser'], cfgdn))
            args['cfgdsuser'] = dsuser
        conn.unbind()
        conn = lib389.DirSrv(
            args['cfgdshost'], args['cfgdsport'], args['cfgdsuser'],
            args['cfgdspwd'], None)
    return conn


def update_admin_domain(isLocal, args):
    """Get the admin domain to use."""
    if isLocal and 'admin_domain' not in args:
        if 'admconf' in args:
            args['admin_domain'] = args['admconf'].admindomain
        elif 'sroot' in args:
            dsconf = open('%s/shared/config/ds.conf' % args['sroot'], 'r')
            for line in dsconf:
                ary = line.split(":")
                if len(ary) > 1 and ary[0] == 'AdminDomain':
                    args['admin_domain'] = ary[1].strip()
            dsconf.close()


def getoldcfgdsinfo(args):
    """Use the old style sroot/shared/config/dbswitch.conf to get the info"""
    dbswitch = open("%s/shared/config/dbswitch.conf" % args['sroot'], 'r')
    try:
        matcher = re.compile(r'^directory\s+default\s+')
        for line in dbswitch:
            m = matcher.match(line)
            if m:
                url = LDAPUrl(line[m.end():])
                ary = url.hostport.split(":")
                if len(ary) < 2:
                    ary.append(389)
                else:
                    ary[1] = int(ary[1])
                ary.append(url.dn)
                return ary
    finally:
        dbswitch.close()


def getnewcfgdsinfo(new_instance_arguments):
    """Use the new style prefix /etc/dirsrv/admin-serv/adm.conf.

        new_instance_arguments = {'admconf': obj } where obj.ldapurl != None
    """
    try:
        url = LDAPUrl(new_instance_arguments['admconf'].ldapurl)
    except AttributeError:
        log.error("missing ldapurl attribute in new_instance_arguments: %r" %
                  new_instance_arguments)
        raise

    ary = url.hostport.split(":")
    if len(ary) < 2:
        ary.append(389)
    else:
        ary[1] = int(ary[1])
    ary.append(url.dn)
    return ary


def getcfgdsinfo(new_instance_arguments):
    """Returns a 3-tuple consisting of the host, port, and cfg suffix.

        `new_instance_arguments` = {
            'cfgdshost':
            'cfgdsport':
            'new_style':
        }
    We need the host and port of the configuration directory server in order
    to create an instance.  If this was not given, read the dbswitch.conf file
    to get the information.  This method will raise an exception if the file
    was not found or could not be open.  This assumes new_instance_arguments
    contains the sroot parameter for the server root path.
    """
    try:
        return (new_instance_arguments['cfgdshost'],
                int(new_instance_arguments['cfgdsport']),
                lib389.CFGSUFFIX)
    except KeyError:  # if keys are missing...
        if new_instance_arguments['new_style']:
            return getnewcfgdsinfo(new_instance_arguments)

        return getoldcfgdsinfo(new_instance_arguments)


def getserverroot(cfgconn, isLocal, args):
    """Grab the serverroot from the instance dir of the config ds if the user
    did not specify a server root directory"""
    if cfgconn and 'sroot' not in args and isLocal:
        ent = cfgconn.getEntry(
            DN_CONFIG, ldap.SCOPE_BASE, "(objectclass=*)",
            ['nsslapd-instancedir'])
        if ent:
            args['sroot'] = os.path.dirname(
                ent.getValue('nsslapd-instancedir'))


def getadminport(cfgconn, cfgdn, args):
    """Return a 2-tuple (asport, True) if the admin server is using SSL,
    False otherwise.

    Get the admin server port so we can contact it via http.  We get this from
    the configuration entry using the CFGSUFFIX and cfgconn.  Also get any
    other information we may need from that entry.
    """
    asport = 0
    secure = False
    if cfgconn:
        dn = cfgdn
        if 'admin_domain' in args:
            dn = "cn=%s,ou=%s, %s" % (
                args[SER_HOST], args['admin_domain'], cfgdn)
        filt = "(&(objectclass=nsAdminServer)(serverHostName=%s)" % args[
            SER_HOST]
        if 'sroot' in args:
            filt += "(serverRoot=%s)" % args['sroot']
        filt += ")"
        ent = cfgconn.getEntry(
            dn, ldap.SCOPE_SUBTREE, filt, ['serverRoot'])
        if ent:
            if 'sroot' not in args and ent.serverRoot:
                args['sroot'] = ent.serverRoot
            if 'admin_domain' not in args:
                ary = ldap.explode_dn(ent.dn, 1)
                args['admin_domain'] = ary[-2]
            dn = "cn=configuration, " + ent.dn
            ent = cfgconn.getEntry(dn, ldap.SCOPE_BASE, '(objectclass=*)',
                                   ['nsServerPort',
                                    'nsSuiteSpotUser',
                                    'nsServerSecurity'])
            if ent:
                asport = ent.nsServerPort
                secure = (ent.nsServerSecurity and (
                    ent.nsServerSecurity == 'on'))
                if 'newuserid' not in args:
                    args['newuserid'] = ent.nsSuiteSpotUser
        cfgconn.unbind()
    return asport, secure


def formatInfData(args):
    """Return the .inf data for a silence setup via setup-ds.pl.

        args = {
            # new instance values
            newhost, newuserid, newport, SER_ROOT_DN, newrootpw, newsuffix,

            # The following parameters require to register the new instance
            # in the admin server
            have_admin, cfgdshost, cfgdsport, cfgdsuser,cfgdspwd, admin_domain

            InstallLdifFile, AddOrgEntries, ConfigFile, SchemaFile,
            ldapifilepath

            # Setup the o=NetscapeRoot namingContext
            setup_admin,
        }

        @see https://access.redhat.com/site/documentation/en-US/
             Red_Hat_Directory_Server/8.2/html/Installation_Guide/
             Installation_Guide-Advanced_Configuration-Silent.html
        [General]
        FullMachineName= dir.example.com
        SuiteSpotUserID= nobody
        SuiteSpotGroup= nobody
        AdminDomain= example.com
        ConfigDirectoryAdminID= admin
        ConfigDirectoryAdminPwd= admin
        ConfigDirectoryLdapURL= ldap://dir.example.com:389/o=NetscapeRoot

        [slapd]
        SlapdConfigForMC= Yes
        UseExistingMC= 0
        ServerPort= 389
        ServerIdentifier= dir
        Suffix= dc=example,dc=com
        RootDN= cn=Directory Manager
        RootDNPwd= password
        ds_bename=exampleDB
        AddSampleEntries= No

        [admin]
        Port= 9830
        ServerIpAddress= 111.11.11.11
        ServerAdminID= admin
        ServerAdminPwd= admin

    """
    args = args.copy()
    args['CFGSUFFIX'] = CFGSUFFIX

    content = ("[General]" "\n")
    content += ("FullMachineName= %s\n" % args[SER_HOST])
    content += ("SuiteSpotUserID= %s\n" % args[SER_USER_ID])
    content += ("nSuiteSpotGroup= %s\n" % args[SER_GROUP_ID])
    content += ("StrictHostCheck= %s\n" % args[SER_STRICT_HOSTNAME_CHECKING])

    if args.get('have_admin'):
        content += ("AdminDomain= %(admin_domain)s" "\n"
                    "ConfigDirectoryLdapURL= ldap://%(cfgdshost)s:%"
                    "(cfgdsport)d/%(CFGSUFFIX)s" "\n"
                    "ConfigDirectoryAdminID= %(cfgdsuser)s" "\n"
                    "ConfigDirectoryAdminPwd= %(cfgdspwd)s" "\n") % args

    content += ("\n" "\n" "[slapd]" "\n")
    content += ("ServerPort= %s\n" % args[SER_PORT])
    content += ("RootDN= %s\n" % args[SER_ROOT_DN])
    content += ("RootDNPwd= %s\n" % args[SER_ROOT_PW])
    content += ("ServerIdentifier= %s\n" % args[SER_SERVERID_PROP])
    content += ("Suffix= %s\n" % args[SER_CREATION_SUFFIX])

    # Create admin?
    if args.get('setup_admin'):
        content += ("SlapdConfigForMC= Yes" "\n"
                    "UseExistingMC= 0 " "\n")

    if 'InstallLdifFile' in args:
        content += """\nInstallLdifFile= %s\n""" % args['InstallLdifFile']
    if 'AddOrgEntries' in args:
        content += """\nAddOrgEntries= %s\n""" % args['AddOrgEntries']
    if 'ConfigFile' in args:
        for ff in args['ConfigFile']:
            content += """\nConfigFile= %s\n""" % ff
    if 'SchemaFile' in args:
        for ff in args['SchemaFile']:
            content += """\nSchemaFile= %s\n""" % ff

    if 'ldapifilepath' in args:
        content += "\nldapifilepath=%s\n" % args['ldapifilepath']

    return content


def get_ds_version():
    """Return version of ns-slapd binary, for example
    1.3.4.8 B2016.043.2254"""
    nsslapd = get_sbin_dir() + "/ns-slapd"
    output = subprocess.Popen([nsslapd, "-v"],
                              stdout=subprocess.PIPE).communicate()[0]
    fullver = output.splitlines()[1]
    ver = fullver.split('/')[1]
    return ver


def ds_is_older(ver):
    """Return True if current version of ns-slapd is older than provided
    version"""
    return get_ds_version() < ver


def getDateTime():
    """
    Return the date and time:

        2016-04-21 21:21:00

    """
    return time.strftime("%Y-%m-%d %H:%M:%S")


def socket_check_open(host, port):
    with closing(socket.socket(socket.AF_INET6, socket.SOCK_STREAM)) as sock:
        if sock.connect_ex((host, port)) == 0:
            return True
        else:
            return False


def ensure_bytes(val):
    if MAJOR >= 3 and type(val) != bytes:
        return val.encode()
    return val


def ensure_str(val):
    if MAJOR >= 3 and type(val) != str:
        return val.decode('utf-8')
    return val


def ensure_list_bytes(val):
    if MAJOR >= 3:
        return [ensure_bytes(v) for v in val]
    return val
