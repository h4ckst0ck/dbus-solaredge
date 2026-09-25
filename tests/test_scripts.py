"""Tests of install.sh, uninstall.sh and setup.sh in a sandbox directory tree.

The Venus OS paths are redirected via environment variables, `svc`, the remount script
and the Python interpreter (used for the connection test) are replaced by stubs.
"""

import os
import pathlib
import shutil
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = ['install.sh', 'uninstall.sh', 'setup.sh', 'restart.sh', 'config.sample.ini', 'service']


class Venus(object):
	def __init__(self, tmp_path):
		self.root = tmp_path
		self.base = tmp_path / 'data' / 'dbus-solaredge'
		self.base.mkdir(parents=True)
		for name in FILES:
			source = ROOT / name
			if source.is_dir():
				shutil.copytree(str(source), str(self.base / name))
			else:
				shutil.copy(str(source), str(self.base / name))
		self.rc_local = tmp_path / 'data' / 'rc.local'
		self.service_dir = tmp_path / 'opt' / 'service'
		self.service_tmpfs = tmp_path / 'service'
		self.service_dir.mkdir(parents=True)
		self.service_tmpfs.mkdir()
		self.log = tmp_path / 'calls.log'
		self.log.write_text('')
		bin_dir = tmp_path / 'bin'
		bin_dir.mkdir()
		self.stub(bin_dir / 'svc', 'echo "svc $*" >> %s' % self.log)
		svc = str(bin_dir / 'svc')
		self.stub(tmp_path / 'remount-rw.sh', 'echo remount >> %s' % self.log)
		self.stub(tmp_path / 'python3', 'echo "python $*" >> %s; echo "inverter: SolarEdge SE10K"; '
			'exit ${CHECK_RESULT:-0}' % self.log)
		self.env = dict(os.environ, RC_LOCAL=str(self.rc_local), SERVICE_DIR=str(self.service_dir),
			SERVICE_TMPFS=str(self.service_tmpfs), REMOUNT_RW=str(tmp_path / 'remount-rw.sh'),
			PYTHON=str(tmp_path / 'python3'), SVC=svc, STOP_DELAY='0', PATH='%s:%s' % (bin_dir, os.environ['PATH']))

	@staticmethod
	def stub(path, body):
		path.write_text('#!/bin/sh\n%s\n' % body)
		path.chmod(0o755)

	def run(self, script, *args, stdin='', env=None, check=True):
		# SHELL_PATH: directory with busybox applets first, like the shell environment of Venus OS
		env = dict(self.env, **(env or {}))
		if os.environ.get('SHELL_PATH'):
			env['PATH'] = '%s:%s' % (os.environ['SHELL_PATH'], env['PATH'])
		result = subprocess.run(['sh', str(script)] + list(args), input=stdin, capture_output=True, text=True,
			env=env)
		if check:
			assert result.returncode == 0, result.stdout + result.stderr
		return result

	def calls(self):
		return self.log.read_text().splitlines()

	def config(self):
		return (self.base / 'config.ini').read_text()

	@property
	def boot_line(self):
		return '[ -x {0}/install.sh ] && {0}/install.sh --boot'.format(self.base)

	def service_linked(self):
		return all(os.readlink(str(d / 'dbus-solaredge')) == str(self.base / 'service')
			for d in (self.service_dir, self.service_tmpfs))


@pytest.fixture
def venus(tmp_path):
	return Venus(tmp_path)


def executable(path):
	return bool(path.stat().st_mode & stat.S_IXUSR)


# ---- install.sh ----
def test_install(venus):
	output = venus.run(venus.base / 'install.sh').stdout
	assert venus.service_linked()
	assert venus.rc_local.read_text() == '#!/bin/sh\n%s\n' % venus.boot_line
	assert executable(venus.rc_local)
	assert (venus.base / 'config.ini').read_text() == (ROOT / 'config.sample.ini').read_text()
	assert executable(venus.base / 'service' / 'run')
	assert venus.calls() == ['remount']
	assert 'installed' in output


def test_install_is_idempotent(venus):
	venus.run(venus.base / 'install.sh')
	(venus.base / 'config.ini').write_text('[modbus]\nhost = 10.0.0.5\n')
	venus.run(venus.base / 'install.sh')
	assert venus.rc_local.read_text().count(venus.boot_line) == 1
	assert venus.calls() == ['remount']  # the root filesystem is only touched when the link is missing
	assert venus.config() == '[modbus]\nhost = 10.0.0.5\n'


def test_install_keeps_existing_rc_local_and_makes_it_executable(venus):
	venus.rc_local.write_text('#!/bin/sh\n/data/other/start.sh\n')
	venus.rc_local.chmod(0o644)
	venus.run(venus.base / 'install.sh')
	assert venus.rc_local.read_text() == '#!/bin/sh\n/data/other/start.sh\n%s\n' % venus.boot_line
	assert executable(venus.rc_local)  # Venus OS ignores rc.local otherwise


def test_firmware_update(venus):
	"""A firmware update replaces the root filesystem, /data and rc.local survive."""
	venus.run(venus.base / 'install.sh')
	os.remove(str(venus.service_dir / 'dbus-solaredge'))
	os.remove(str(venus.service_tmpfs / 'dbus-solaredge'))
	result = venus.run(venus.rc_local)  # boot
	assert venus.service_linked()
	assert result.stdout == ''
	assert venus.calls() == ['remount', 'remount']


def test_reboot_without_update(venus):
	venus.run(venus.base / 'install.sh')
	os.remove(str(venus.service_tmpfs / 'dbus-solaredge'))  # tmpfs is empty after a reboot
	venus.run(venus.rc_local)
	assert venus.service_linked()
	assert venus.calls() == ['remount']


def test_boot_line_ignores_removed_installation(venus):
	venus.run(venus.base / 'install.sh')
	shutil.rmtree(str(venus.base))
	venus.rc_local.write_text(venus.rc_local.read_text() + 'echo next >> %s\n' % venus.log)
	venus.run(venus.rc_local)
	assert venus.calls()[-1] == 'next'


def test_install_with_disabled_modifications(venus):
	disabled = pathlib.Path(str(venus.rc_local) + '.disabled')
	disabled.write_text('#!/bin/sh\n/data/other/start.sh\n')
	output = venus.run(venus.base / 'install.sh').stdout
	assert not venus.rc_local.exists()
	assert disabled.read_text().endswith(venus.boot_line + '\n')
	assert 'WARNING' in output and 'Modification checks' in output


# ---- uninstall.sh ----
def test_uninstall(venus):
	venus.rc_local.write_text('#!/bin/sh\n/data/other/start.sh\n')
	venus.run(venus.base / 'install.sh')
	venus.run(venus.base / 'uninstall.sh')
	assert venus.rc_local.read_text() == '#!/bin/sh\n/data/other/start.sh\n'
	assert not os.path.lexists(str(venus.service_dir / 'dbus-solaredge'))
	assert not os.path.lexists(str(venus.service_tmpfs / 'dbus-solaredge'))
	assert 'svc -d %s/dbus-solaredge' % venus.service_tmpfs in venus.calls()
	assert (venus.base / 'config.ini').exists()


def test_uninstall_from_disabled_hook(venus):
	disabled = pathlib.Path(str(venus.rc_local) + '.disabled')
	disabled.write_text('#!/bin/sh\n')
	venus.run(venus.base / 'install.sh')
	venus.run(venus.base / 'uninstall.sh')
	assert disabled.read_text() == '#!/bin/sh\n'


# ---- setup.sh ----
def test_setup(venus):
	output = venus.run(venus.base / 'setup.sh', stdin='10.0.0.5\n1502\n1\nno\n').stdout
	config = venus.config()
	assert 'host = 10.0.0.5\n' in config
	assert 'port = 1502\n' in config
	assert 'unit = 1\n' in config
	assert 'power_limit = no\n' in config
	assert 'power_limit_timeout = 120\n' in config  # other options and comments are kept
	assert '; IP address of the SolarEdge inverter' in config
	assert 'python %s/dbus-solaredge.py -c %s/config.ini --check' % (venus.base, venus.base) in venus.calls()
	assert 'inverter: SolarEdge SE10K' in output
	assert venus.service_linked()
	assert venus.boot_line in venus.rc_local.read_text()


def test_setup_keeps_values_on_enter(venus):
	venus.run(venus.base / 'setup.sh', stdin='10.0.0.5\n1502\n1\nno\n')
	venus.run(venus.base / 'setup.sh', stdin='\n\n\n\n')
	config = venus.config()
	assert 'host = 10.0.0.5\n' in config and 'port = 1502\n' in config and 'unit = 1\n' in config
	assert 'power_limit = no\n' in config


def test_setup_asks_again_on_invalid_input(venus):
	output = venus.run(venus.base / 'setup.sh', stdin='10.0.0.5 x\n10.0.0.5\nabc\n70000\n502\n0\n248\n2\n'
		'maybe\ny\n').stdout
	assert output.count('please enter an IP address') == 1
	assert output.count('please enter a number between 1 and 65535') == 2
	assert output.count('please enter a number between 1 and 247') == 2
	assert output.count('please answer yes or no') == 1
	config = venus.config()
	assert 'host = 10.0.0.5\n' in config and 'port = 502\n' in config and 'unit = 2\n' in config
	assert 'power_limit = yes\n' in config


def test_setup_adds_missing_options(venus):
	(venus.base / 'config.ini').write_text('[modbus]\nhost = 10.0.0.9\n')
	venus.run(venus.base / 'setup.sh', stdin='\n502\n5\nyes\n')
	assert venus.config() == '[modbus]\nunit = 5\nport = 502\nhost = 10.0.0.9\n\n[inverter]\npower_limit = yes\n'


def test_setup_stops_and_starts_running_service(venus):
	venus.run(venus.base / 'install.sh')
	(venus.service_tmpfs / 'dbus-solaredge' / 'supervise').mkdir()
	venus.run(venus.base / 'setup.sh', stdin='\n\n\n\n')
	service = '%s/dbus-solaredge' % venus.service_tmpfs
	calls = [c for c in venus.calls() if c != 'remount']
	assert calls == ['svc -d %s' % service, 'python %s/dbus-solaredge.py -c %s/config.ini --check'
		% (venus.base, venus.base), 'svc -u %s' % service]


def test_setup_connection_failed_abort(venus):
	result = venus.run(venus.base / 'setup.sh', stdin='\n\n\n\nn\n', env={'CHECK_RESULT': '1'}, check=False)
	assert result.returncode == 1
	assert 'not reachable' in result.stdout
	assert not venus.rc_local.exists()
	assert not os.path.lexists(str(venus.service_tmpfs / 'dbus-solaredge'))


def test_setup_connection_failed_install_anyway(venus):
	venus.run(venus.base / 'setup.sh', stdin='\n\n\n\ny\n', env={'CHECK_RESULT': '1'})
	assert venus.service_linked()


def test_setup_without_input(venus):
	"""EOF on stdin takes the defaults."""
	venus.run(venus.base / 'setup.sh', stdin='')
	assert 'host = 192.168.178.80\n' in venus.config()
	assert venus.service_linked()


def test_restart(venus):
	venus.run(venus.base / 'restart.sh')
	assert venus.calls() == ['svc -t /service/dbus-solaredge']
