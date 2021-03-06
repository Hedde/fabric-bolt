#!/usr/bin/env python
import os, sys


if __name__ == '__main__':
    from gevent import monkey
    monkey.patch_all()


    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fabric_bolt.core.settings.local')
    sys.path.append(os.getcwd())

    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
