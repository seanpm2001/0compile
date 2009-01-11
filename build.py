# Copyright (C) 2006, Thomas Leonard
# See http://0install.net/0compile.html

import sys, os, __main__, time, shutil, glob, codecs
from os.path import join
from logging import info
from xml.dom import minidom, XMLNS_NAMESPACE
from optparse import OptionParser

from support import *

def env(name, value):
	os.environ[name] = value
	print "%s=%s" % (name, value)

def do_env_binding(binding, path):
	env(binding.name, binding.get_value(path, os.environ.get(binding.name, None)))

def do_build_internal(options, args):
	"""build-internal"""
	# If a sandbox is being used, we're in it now.
	import getpass, socket, time

	buildenv = BuildEnv()
	sels = buildenv.get_selections()

	builddir = os.path.realpath('build')
	ensure_dir(buildenv.metadir)

	build_env_xml = join(buildenv.metadir, 'build-environment.xml')

	buildenv_doc = buildenv.get_selections().toDOM()

	# Create build-environment.xml file
	root = buildenv_doc.documentElement
	info = buildenv_doc.createElementNS(XMLNS_0COMPILE, 'build-info')
	root.appendChild(info)
	info.setAttributeNS(None, 'time', time.strftime('%Y-%m-%d %H:%M').strip())
	info.setAttributeNS(None, 'host', socket.getfqdn())
	info.setAttributeNS(None, 'user', getpass.getuser())
	uname = os.uname()
	info.setAttributeNS(None, 'arch', '%s-%s' % (uname[0], uname[4]))
	stream = file(build_env_xml, 'w')
	buildenv_doc.writexml(stream, addindent="  ", newl="\n")
	stream.close()

	# Create local binary interface file
	src_iface = iface_cache.get_interface(buildenv.interface)
	src_impl = buildenv.chosen_impl(buildenv.interface)
	write_sample_interface(buildenv, src_iface, src_impl)

	# Check 0compile is new enough
	min_version = parse_version(src_impl.attrs.get(XMLNS_0COMPILE + ' min-version', None))
	if min_version and min_version > parse_version(__main__.version):
		raise SafeException("%s-%s requires 0compile >= %s, but we are only version %s" %
				(src_iface.get_name(), src_impl.version, format_version(min_version), __main__.version))

	# Create the patch
	patch_file = join(buildenv.metadir, 'from-%s.patch' % src_impl.version)
	if buildenv.user_srcdir:
		# (ignore errors; will already be shown on stderr)
		os.system("diff -urN '%s' src > %s" %
			(buildenv.orig_srcdir.replace('\\', '\\\\').replace("'", "\\'"),
			 patch_file))
		if os.path.getsize(patch_file) == 0:
			os.unlink(patch_file)
	elif os.path.exists(patch_file):
		os.unlink(patch_file)

	env('BUILDDIR', builddir)
	env('DISTDIR', buildenv.distdir)
	env('SRCDIR', buildenv.user_srcdir or buildenv.orig_srcdir)
	os.chdir(builddir)
	print "cd", builddir

	for needed_iface in sels.selections:
		impl = buildenv.chosen_impl(needed_iface)
		assert impl
		for dep in impl.dependencies:
			dep_iface = sels.selections[dep.interface]
			for b in dep.bindings:
				if isinstance(b, EnvironmentBinding):
					dep_impl = buildenv.chosen_impl(dep.interface)
					do_env_binding(b, lookup(dep_impl.id))

	mappings = []
	for impl in sels.selections.values():
		new_mappings = impl.attrs.get(XMLNS_0COMPILE + ' lib-mappings', '')
		if new_mappings:
			new_mappings = new_mappings.split(' ')
			for mapping in new_mappings:
				assert ':' in mapping, "lib-mappings missing ':' in '%s' from '%s'" % (mapping, impl.feed)
				name, major_version = mapping.split(':', 1)
				assert '/' not in mapping, "lib-mappings '%s' contains a / in the version number (from '%s')!" % (mapping, impl.feed)
				mappings.append((name, major_version))
	
	if mappings:
		set_up_mappings(mappings)

	# Some programs want to put temporary build files in the source directory.
	# Make a copy of the source if needed.
	dup_src_type = src_impl.attrs.get(XMLNS_0COMPILE + ' dup-src', None)
	if dup_src_type == 'true':
		dup_src(shutil.copy2)
		env('SRCDIR', builddir)
	elif dup_src_type:
		raise Exception("Unknown dup-src value '%s'" % dup_src_type)

	if options.shell:
		spawn_and_check(find_in_path('sh'), [])
	else:
		command = src_impl.attrs[XMLNS_0COMPILE + ' command']

		# Remove any existing log files
		for log in ['build.log', 'build-success.log', 'build-failure.log']:
			if os.path.exists(log):
				os.unlink(log)

		# Run the command, copying output to a new log
		log = file('build.log', 'w')
		try:
			print >>log, "Build log for %s-%s" % (src_iface.get_name(),
							      src_impl.version)
			print >>log, "\nBuilt using 0compile-%s" % __main__.version
			print >>log, "\nBuild system: " + ', '.join(uname)
			print >>log, "\n%s:\n" % ENV_FILE
			shutil.copyfileobj(file("../" + ENV_FILE), log)

			log.write('\n')

			if os.path.exists(patch_file):
				print >>log, "\nPatched with:\n"
				shutil.copyfileobj(file(patch_file), log)
				log.write('\n')

			print "Executing: " + command
			print >>log, "Executing: " + command

			# Tee the output to the console and to the log
			from popen2 import Popen4
			child = Popen4(command)
			child.tochild.close()
			while True:
				data = os.read(child.fromchild.fileno(), 100)
				if not data: break
				sys.stdout.write(data)
				log.write(data)
			status = child.wait()
			failure = None
			if os.WIFEXITED(status):
				exit_code = os.WEXITSTATUS(status)
				if exit_code == 0:
					print >>log, "Build successful"
				else:
					failure = "Build failed with exit code %d" % exit_code
			else:
				failure = "Build failure: exited due to signal %d" % os.WTERMSIG(status)
			if failure:
				print >>log, failure
				os.rename('build.log', 'build-failure.log')
				raise SafeException("Command '%s': %s" % (command, failure))
			else:
				os.rename('build.log', 'build-success.log')
		finally:
			log.close()

def do_build(args):
	"""build [ --no-sandbox ] [ --shell | --force | --clean ]"""
	buildenv = BuildEnv()
	sels = buildenv.get_selections()

	parser = OptionParser(usage="usage: %prog build [options]")

	parser.add_option('', "--no-sandbox", help="disable use of sandboxing", action='store_true')
	parser.add_option("-s", "--shell", help="run a shell instead of building", action='store_true')
	parser.add_option("-c", "--clean", help="remove the build directories", action='store_true')
	parser.add_option("-f", "--force", help="build even if dependencies have changed", action='store_true')

	parser.disable_interspersed_args()

	(options, args2) = parser.parse_args(args)

	builddir = os.path.realpath('build')

	changes = buildenv.get_build_changes()
	if changes:
		if not (options.force or options.clean):
			raise SafeException("Build dependencies have changed:\n" +
					'\n'.join(changes) + "\n\n" +
					"To build anyway, use: 0compile build --force\n" +
					"To do a clean build:  0compile build --clean")
		if not options.no_sandbox:
			print "Build dependencies have changed:\n" + '\n'.join(changes)

	ensure_dir(builddir, options.clean)
	ensure_dir(buildenv.distdir, options.clean)

	if options.no_sandbox:
		return do_build_internal(options, args2)

	tmpdir = tempfile.mkdtemp(prefix = '0compile-')
	try:
		my_dir = os.path.dirname(__file__)
		readable = ['.', my_dir]
		writable = ['build', buildenv.distdir, tmpdir]
		env('TMPDIR', tmpdir)

		for selection in sels.selections.values():
			readable.append(lookup(selection.id))

		options = []
		if __main__.options.verbose:
			options.append('--verbose')

		readable.append('/etc')	# /etc/ld.*

		spawn_maybe_sandboxed(readable, writable, tmpdir, sys.executable, [sys.argv[0]] + options + ['build', '--no-sandbox'] + args)
	finally:
		info("Deleting temporary directory '%s'" % tmpdir)
		shutil.rmtree(tmpdir)

def write_sample_interface(buildenv, iface, src_impl):
	path = buildenv.local_iface_file
	target_arch = buildenv.target_arch

	impl = minidom.getDOMImplementation()

	XMLNS_IFACE = namespaces.XMLNS_IFACE

	doc = impl.createDocument(XMLNS_IFACE, "interface", None)

	root = doc.documentElement
	root.setAttributeNS(XMLNS_NAMESPACE, 'xmlns', XMLNS_IFACE)

	def addSimple(parent, name, text = None):
		elem = doc.createElementNS(XMLNS_IFACE, name)

		parent.appendChild(doc.createTextNode('\n' + '  ' * (1 + depth(parent))))
		parent.appendChild(elem)
		if text:
			elem.appendChild(doc.createTextNode(text))
		return elem

	def close(element):
		element.appendChild(doc.createTextNode('\n' + '  ' * depth(element)))

	addSimple(root, 'name', iface.name)
	addSimple(root, 'summary', iface.summary)
	addSimple(root, 'description', iface.description)
	feed_for = addSimple(root, 'feed-for')

	uri = iface.uri
	if uri.startswith('/') and iface.feed_for:
		for uri in iface.feed_for:
			print "Note: source %s is a local feed" % iface.uri
			print "Will use <feed-for interface='%s'> instead..." % uri
			break

	feed_for.setAttributeNS(None, 'interface', uri)

	group = addSimple(root, 'group')
	main = src_impl.attrs.get(XMLNS_0COMPILE + ' binary-main', None)
	if main:
		group.setAttributeNS(None, 'main', main)

	lib_mappings = src_impl.attrs.get(XMLNS_0COMPILE + ' binary-lib-mappings', None)
	if lib_mappings:
		root.setAttributeNS(XMLNS_NAMESPACE, 'xmlns:compile', XMLNS_0COMPILE)
		group.setAttributeNS(XMLNS_0COMPILE, 'compile:lib-mappings', lib_mappings)
	
	for d in src_impl.dependencies:
		# 0launch < 0.32 messed up the namespace...
		if parse_bool(d.metadata.get('include-binary', 'false')) or \
		   parse_bool(d.metadata.get(XMLNS_0COMPILE + ' include-binary', 'false')):
			requires = addSimple(group, 'requires')
			requires.setAttributeNS(None, 'interface', d.interface)
			for b in d.bindings:
				if isinstance(b, model.EnvironmentBinding):
					env_elem = addSimple(requires, 'environment')
					env_elem.setAttributeNS(None, 'name', b.name)
					env_elem.setAttributeNS(None, 'insert', b.insert)
					if b.default:
						env_elem.setAttributeNS(None, 'default', b.default)
				else:
					raise Exception('Unknown binding type ' + b)
			close(requires)
				
	group.setAttributeNS(None, 'arch', target_arch)
	impl_elem = addSimple(group, 'implementation')
	impl_elem.setAttributeNS(None, 'version', src_impl.version)

	version_modifier = buildenv.version_modifier
	if version_modifier:
		impl_elem.setAttributeNS(None, 'version-modifier', version_modifier)

	impl_elem.setAttributeNS(None, 'id', '..')
	impl_elem.setAttributeNS(None, 'released', time.strftime('%Y-%m-%d'))
	close(group)
	close(root)

	stream = codecs.open(path, 'w', encoding = 'utf-8')
	try:
		doc.writexml(stream)
	finally:
		stream.close()

def set_up_mappings(mappings):
	"""Create a temporary directory with symlinks for each of the library mappings."""
	# The find_library function takes a short-name and major version of a library and
	# returns the full path of the library.
	libdirs = ['/lib', '/usr/lib']
	for d in os.environ.get('LD_LIBRARY_PATH', '').split(':'):
		if d: libdirs.append(d)

	def add_ldconf(config_file):
		if not os.path.isfile(config_file):
			return
		for line in file(config_file):
			d = line.strip()
			if d.startswith('include '):
				glob_pattern = d.split(' ', 1)[1]
				for conf in glob.glob(glob_pattern):
					add_ldconf(conf)
			elif d and not d.startswith('#'):
				libdirs.append(d)
	add_ldconf('/etc/ld.so.conf')

	def find_library(name, major):
		wanted = 'lib%s.so.%s' % (name, major)
		for d in libdirs:
			path = os.path.join(d, wanted)
			if os.path.exists(path):
				return path
		print "WARNING: library '%s' not found (searched '%s')!" % (wanted, libdirs)
		return None

	mappings_dir = os.path.join(os.environ['TMPDIR'], 'lib-mappings')
	os.mkdir(mappings_dir)

	old_path = os.environ.get('LIBRARY_PATH', '')
	if old_path: old_path = ':' + old_path
	os.environ['LIBRARY_PATH'] = mappings_dir + old_path

	for name, major_version in mappings:
		target = find_library(name, major_version)
		if target:
			print "Adding mapping lib%s.so -> %s" % (name, target)
			os.symlink(target, os.path.join(mappings_dir, 'lib' + name + '.so'))

def dup_src(fn):
	srcdir = os.environ['SRCDIR'] + '/'
	for root, dirs, files in os.walk(srcdir):
		assert root.startswith(srcdir)
		reldir = root[len(srcdir):]
		for f in files:
			target = os.path.join(reldir, f)
			#print "Copy %s -> %s" % (os.path.join(root, f), target)
			if os.path.exists(target):
				os.unlink(target)
			fn(os.path.join(root, f), target)
		for d in dirs:
			target = os.path.join(reldir, d)
			if not os.path.isdir(target):
				os.mkdir(target)

__main__.commands.append(do_build)
