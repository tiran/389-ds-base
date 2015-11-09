#!/usr/bin/python

from clitools import CliTool, clitools_parser
from lib389._constants import *
from lib389.mit_krb5 import MitKrb5
from argparse import ArgumentParser

class MitKrb5Tool(CliTool):
    def mit_krb5_realm_create(self):
        try:
            krb = MitKrb5(realm=args.realm, warnings=True)
            krb.create_keytab(principal=args.principal, keytab=args.keytab)
        finally:
            pass

if __name__ == '__main__':
    # Do some arg parse stuff
    ## You can always add a child parser here too ...
    parser = ArgumentParser(parents=[clitools_parser])
    parser.add_argument('--realm', '-r', help='The name of the realm to create', required=True)
    parser.add_argument('--principal', '-p', help='The name of the principal to create', required=True)
    parser.add_argument('--keytab', '-k', help='The path to the keytab to create', required=True)
    args = parser.parse_args()
    mittool = MitKrb5Tool(args)
    mittool.mit_krb5_realm_create()