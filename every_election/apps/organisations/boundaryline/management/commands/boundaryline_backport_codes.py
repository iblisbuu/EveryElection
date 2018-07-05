"""
Pass in either a directory path to an already extracted
copy of BoundaryLine or a path to a zipped copy.

For example:
manage.py boundaryline_backport_codes -f /foo/bar/bdline_gb-2018-05
manage.py boundaryline_backport_codes -f /foo/bar/bdline_gb-2018-05.zip
manage.py boundaryline_backport_codes -u "http://parlvid.mysociety.org/os/bdline_gb-2018-05.zip"

are all valid calls.
"""

import argparse
import os
import shutil
from collections import namedtuple
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from core.mixins import ReadFromFileMixin
from storage.zipfile import unzip
from organisations.models import Organisation, OrganisationDivision
from organisations.boundaryline.constants import ORG_TYPES
from organisations.boundaryline import BoundaryLine


class Command(ReadFromFileMixin, BaseCommand):

    help = """
    Use BoundaryLine to try and retrospectively attach codes
    to divisions imported from LGBCE with pseudo-identifiers.
    """

    cleanup_required = False
    found = []
    not_found = []
    Record = namedtuple('Record', ['division', 'code'])
    org_boundaries = {}

    def add_arguments(self, parser):

        def check_valid_date(value):
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                raise argparse.ArgumentTypeError(
                    "Not a valid date: '{0}'.".format(value)
                )

        parser.add_argument(
            'date',
            action='store',
            help='Reference date for BoundaryLine release',
            type=check_valid_date
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            dest='dry-run',
            help="Don't commit changes"
        )
        super().add_arguments(parser)

    def get_divisions(self, types, date):
        return OrganisationDivision.objects.filter(
            division_type__in=types,
            divisionset__start_date__lte=date
        ).extra(where=[
            "LEFT(organisations_organisationdivision.official_identifier,4) != 'gss:'",
            "LEFT(organisations_organisationdivision.official_identifier,8) != 'unit_id:'",
            "LEFT(organisations_organisationdivision.official_identifier,9) != 'osni_oid:'",
        ]).order_by(
            'official_identifier'
        ).select_related(
            'divisionset'
        )

    def get_parent_org_boundary(self, div):
        """
        For performance reasons, we cache these results in a dict indexed by
        (organisation_id, divisionset.start_date)
        This means we only need to do one round-trip to the DB per DivisionSet
        instead of once per division. This speeds things up a bit.
        """
        if (div.organisation_id, div.divisionset.start_date) in self.org_boundaries:
            return self.org_boundaries[(div.organisation_id, div.divisionset.start_date)]

        org = Organisation.objects.get(
            pk=div.organisation_id
        ).get_geography(
            div.divisionset.start_date
        )
        self.org_boundaries[(div.organisation_id, div.divisionset.start_date)] = org
        return org

    def get_base_dir(self, **options):
        try:
            if options['url']:
                self.stdout.write('Downloading data from %s ...' % (options['url']))
            fh = self.load_data(options)
            self.stdout.write('Extracting archive...')
            path = unzip(fh.name)
            self.stdout.write('...done')

            # if we've extracted a zip file to a temp location
            # we want to delete the temp files when we're done
            self.cleanup_required = True
            return path
        except IsADirectoryError:
            return options['file']

    def cleanup(self, tempdir):
        # clean up the temp files we created
        try:
            shutil.rmtree(tempdir)
        except OSError:
            self.stdout.write("Failed to clean up temp files.")

    @transaction.atomic
    def save_all(self):
        self.stdout.write("Saving...")
        for rec in self.found:
            rec.division.official_identifier = rec.code
            rec.division.save()
        self.stdout.write("...done")

    def report_found(self):
        for rec in self.found:
            self.stdout.write(
                'Found code {code} for division {div}'.format(
                    code=rec.code,
                    div=rec.division.official_identifier)
            )

    def report_not_found(self):
        for rec in self.not_found:
            self.stdout.write(
                'Could not find a code for division {div}'.format(
                    div=rec.division.official_identifier)
            )

    def report(self, verbose):
        self.stdout.write('Searched {} divisions'.format(
            len(self.found) + len(self.not_found))
        )
        self.stdout.write('Found {} codes'.format(len(self.found)))
        self.stdout.write("\n")
        if verbose:
            self.report_found()
        self.stdout.write("\n")
        self.report_not_found()

    def handle(self, *args, **options):
        base_dir = self.get_base_dir(**options)

        self.stdout.write('Searching...')
        for org_type, filename in ORG_TYPES.items():
            bl = BoundaryLine(os.path.join(base_dir, 'Data', 'GB', filename))
            divs = self.get_divisions(org_type, options['date'])
            for div in divs:
                org = self.get_parent_org_boundary(div)
                code = bl.get_division_code(div, org)
                if code:
                    self.found.append(self.Record(div, code))
                else:
                    self.not_found.append(self.Record(div, code))

        verbose = options['verbosity'] > 1
        self.report(verbose)

        if not options['dry-run']:
            self.save_all()

        if self.cleanup_required:
            self.cleanup(base_dir)

        self.stdout.write('...done!')
