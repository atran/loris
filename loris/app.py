#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
`loris.app` -- WSGI JPEG 2000 Server
====================================
Implements IIIF 1.0 <http://www-sul.stanford.edu/iiif/image-api> level 1 and 
most of level 2 (all but return of JPEG 2000 derivatives).

Author: Jon Stroop <jstroop@princeton.edu>
Since: 2012-08-25

	Copyright (C) 2012  The Trustees of Princeton University

	This program is free software: you can redistribute it and/or modify it 
	under the terms of the GNU General Public License as published by the Free 
	Software Foundation, either version 3 of the License, or (at your option) 
	any later version.

	This program is distributed in the hope that it will be useful, but WITHOUT 
	ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or 
	FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for 
	more details.

	You should have received a copy of the GNU General Public License along 
	with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
from datetime import datetime
from decimal import Decimal, getcontext
from loris.constants import COMPLIANCE, HELP
from loris.converters import RegionConverter, SizeConverter, RotationConverter
from loris.exceptions import LorisException
from loris.img_info import ImgInfo
from loris.parameters import RegionParameter, SizeParameter, RotationParameter
from random import choice
from string import ascii_lowercase, digits
from werkzeug.datastructures import Headers
from werkzeug.http import http_date, parse_date
from werkzeug.routing import Map, Rule
from werkzeug.wrappers import Request, Response
from werkzeug.wsgi import SharedDataMiddleware
import ConfigParser
import logging
import logging.config
import loris.resolver as resolver
import os
import subprocess
import sys

ETC = '/etc/loris'

def create_app(test=False):
	"""Creates an instance of the `Loris`.

	This method should be used by WSGI to create instances of `Loris`, which 
	in turn implements the WSGI application interface (via `Loris.__call__`),

	More about how to configure and deploy WSGI applications can be found 
	here: <http://code.google.com/p/modwsgi/wiki/QuickConfigurationGuide>.

	Args:
		test (bool): Generally for unit tests, changes from configured dirs to 
			test dirs.
	"""
	global ETC
	global TMP
	global CACHE
	global WWW

	ETC = ETC
	try:
		conf = ConfigParser.RawConfigParser()
		if test:
			root = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
			conf_file_path = os.path.join(root, 'etc', 'loris.conf')
			
			conf.read(conf_file_path)
			WWW = os.path.join(root, 'www')
			TMP = conf.get('directories', 'test_tmp')
			CACHE = conf.get('directories', 'test_cache_root')
			for d in (TMP, CACHE):
				if not os.path.exists(d):
					os.makedirs(d)
		else:
			conf_file_path = os.path.join(ETC, 'loris.conf')
			conf = ConfigParser.RawConfigParser()
			conf.read(conf_file_path)
			WWW = conf.get('directories', 'www')
			TMP = conf.get('directories', 'tmp')
			CACHE = conf.get('directories', 'cache_root')

		app = Loris(test)

		return app

	except IOError, ie:
		sys.stderr.write(str(ie) + ' Maybe the log (or dir) is not writable?\n')
		sys.exit(1)
	except Exception, ex:
		sys.stderr.write(ex.message + '\n')
		sys.stderr.write(str(ex) + '\n')
		sys.exit(1)


class Loris(object):
	"""The application. Generally these should be instantiated with the module
	function `create_app`.

	This is the WSGI application interface (see `__call__`).

	Attributes:
		test (bool): See below.
		decimal_precision (int): The number of decimal places to use when
			converting pixel-based region requests to decimal numbers (for 
			kdu shell outs).
		cache_px_only (bool): If True pct-based requests will be cached as 
			though they were pixel-based, thus saving space in the cache.
		default_format (str): Default format for image requests when it 
			cannot be determined from the URI or HTTP Accept header. MUST be
			'jpg' or 'png'.
		default_info_format (str): Default format for ifo requests when it 
			cannot be determined from the URI or HTTP Accept header. MUST be
			'xml' or 'json'.
		enable_cache (bool): If True, cache images and marshalled info 
			objects to 	the file system.
		convert_cmd (str): Absolute path on the file system to the 
			ImageMagick `convert` binary.
		info_cache (InfoCache): An instance of InfoCache used to stop ImgInfo
			objects.
		mkfifo_cmd (str): Absolute path on the file system to the 
			`mkfifo` utility.
		kdu_expand_cmd (str): Absolute path on the file system to the 
			`kdu_expand` binary.
		kdu_libs (str): Absolute path on the file system to the directory
			containing the Kakadu shared object (`.so`) files.
		rm_cmd (str): Absolute path on the file system to the `rm` utility.
	"""
	def __init__(self, test=False):
		"""Read in the configuration file and calculate attributes.

		Kwargs:
			test (bool): Primarily for unit tests, changes from configured dirs 
				to test dirs.
		"""
		self.test = test
		self.loggr = None
		conf_file_path = None
		log_conf_file_path  = None
		# Configuration
		if self.test:
			# conf
			root = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
			conf_file_path = os.path.join(root, 'etc', 'loris.conf')
			# log
			log_conf_file_path = os.path.join(root, 'etc', 'dev_logging.conf')
			self.loggr = logging.getLogger('loris_dev')
			# resolver
			resolver.SRC_IMG_ROOT = os.path.join(root, 'test', 'img')
		else:
			conf_file_path = os.path.join(ETC, 'loris.conf')
			log_conf_file_path = os.path.join(ETC, 'logging.conf')
			self.loggr = logging.getLogger('loris')
		 
		logging.config.fileConfig(log_conf_file_path)
		_conf = ConfigParser.RawConfigParser()
		_conf.read(conf_file_path)

		# options
		self.decimal_precision = _conf.getint('options', 'decimal_precision')
		getcontext().prec = self.decimal_precision
		self.cache_px_only = _conf.getboolean('options', 'cache_px_only')
		self.default_format = _conf.get('options', 'default_format')
		self.default_info_format = _conf.get('options', 'default_info_format')
		self.enable_cache = _conf.getboolean('options', 'enable_cache')
		self.allow_callback = _conf.getboolean('options', 'allow_callback')

		self.enable_info_cache = _conf.getboolean('options', 'enable_cache')
		if self.enable_info_cache:
			info_cache_size = _conf.getint('options', 'info_cache_size')
			self.info_cache = InfoCache(info_cache_size)

		# utilities
		self.convert_cmd = _conf.get('utilities', 'convert')
		self._convert_libs = _conf.get('utilities', 'convert_libs')
		self.kdu_expand_cmd = _conf.get('utilities', 'kdu_expand')
		self._kdu_libs = _conf.get('utilities', 'kdu_libs')
		self.env = {"LD_LIBRARY_PATH":self._kdu_libs, "PATH":self._convert_libs}
		self.mkfifo_cmd = _conf.get('utilities', 'mkfifo')
		self.rm_cmd = _conf.get('utilities', 'rm')

		# compliance and help links
		self._link_hdr = '<' + COMPLIANCE  + '>;rel=profile,'
		self._link_hdr += '<' + HELP + '>;rel=help'

		_converters = {
				'region' : RegionConverter,
				'size' : SizeConverter,
				'rotation' : RotationConverter
			}
		self._url_map = Map([
			Rule('/<path:ident>/info.<fmt>', endpoint='get_img_metadata'),
			Rule('/<path:ident>/info', endpoint='get_img_metadata'),
			Rule('/<path:ident>/<region:region>/<size:size>/<rotation:rotation>/<any(native, color, grey, bitonal):quality>.<fmt>', endpoint='get_img'),
			Rule('/<path:ident>/<region:region>/<size:size>/<rotation:rotation>/<any(native, color, grey, bitonal):quality>', endpoint='get_img'),
			Rule('/', endpoint='get_docs'),
			Rule('/favicon.ico', endpoint='get_favicon')
		], converters=_converters)
	
	def _dispatch_request(self, request):
		"""Dispatch the request to the proper method. 

		By convention, the endpoint, (i.e. the method to be called) is named 
		'on_<method>', e.g. `on_get_img_metadata`, `on_get_img`,etc. These all 
		must return Response objects.

		Args:
			request (Request): The client's request.

		Returns:
			Response. Varies based on the method to which the `request` was 
			routed, but even Exceptions should result in an Response with an
			XML body. See IIIF 6.2 Error Conditions 
			<http://www-sul.stanford.edu/iiif/image-api/#error>.
		"""
		adapter = self._url_map.bind_to_environ(request.environ)
		try:
			endpoint, values = adapter.match()
			dispatch_to_method = 'on_' + endpoint
			self.loggr.debug('Dispatching to ' + dispatch_to_method)
			return getattr(self, dispatch_to_method)(request, **values)

		# Any exceptions related to parsing the requests into parameter objects
		# should end up here.
		except LorisException, e:
			mime = 'text/xml'
			status = e.http_status
			resp = e.to_xml()
			headers = Headers()
			headers.add('Link', self._link_hdr)
			self.loggr.exception(e.message)
			return Response(resp, status=status, mimetype=mime, headers=headers)

		except Exception, e:
			pe = LorisException(400, '', e.message)
			mime = 'text/xml'
			status = pe.http_status
			resp = pe.to_xml()
			headers = Headers()
			headers.add('Link', self._link_hdr)
			self.loggr.exception(e.message)
			return Response(resp, status=status, mimetype=mime, headers=headers)

	def on_get_favicon(self, request):
		f = os.path.join(WWW, 'icons', 'loris-icon.png')
		return Response(file(f), content_type='image/x-icon')
	
	def on_get_docs(self, request):
		"""Just so that we have something at the root of the service."""
		docs = os.path.join(WWW, 'index.html')
		return Response(file(docs), mimetype='text/html')

	def on_get_img_metadata(self, request, ident, fmt=None):
		"""Exposes image information.

		See <http://www-sul.stanford.edu/iiif/image-api/#info>

		Args:
			request (Request): The client's request.
			ident (str): The identifier for the image.

		Kwargs:
			format (str): 'json', 'xml'. Default is None, in which case we look
				first at the Accept header, and then the default format set in
				`loris.conf`.

		Returns:
			Response. Body is XML or json, depending on the request, None if 
			304, or XML in the case of an error, per IIIF 6.2
			<http://www-sul.stanford.edu/iiif/image-api/#error>
		"""
		resp = None
		status = None
		mime = None
		headers = Headers()
		headers.add('Link', self._link_hdr)
		headers.add('Cache-Control', 'public')

		callback = request.args.get('callback', None)

		try:
			# really hate all this...
			if fmt == 'json': 
				if callback and self.allow_callback:
					mime = 'application/javascript'
				else:
					mime = 'text/json'
			elif fmt == 'xml': 
				mime = 'text/xml'
			elif request.headers.get('accept') == 'text/json':
				fmt = 'json'
				if callback and self.allow_callback:
					mime = 'application/javascript'
				else:
					mime = 'text/json'
			elif request.headers.get('accept') == 'text/xml':
				fmt = 'xml'
				mime = 'text/xml'
			else: # fmt is None, return the default 
				fmt = self.default_info_format
				mime = 'text/json' if fmt == 'json' else 'text/xml'
				
			img_path = self._resolve_identifier(ident)
			
			if not os.path.exists(img_path):
				msg = 'Identifier does not resolve to an image.'
				raise LorisException(404, ident, msg)
			
			cache_dir = os.path.join(CACHE, ident)
			cache_path = os.path.join(cache_dir, 'info.') + fmt

			# check the cache
			if os.path.exists(cache_path) and self.enable_cache == True:
				status = self._check_cache(cache_path, request, headers)
				if status == 200:
					with open(cache_path) as f:
						resp = f.read()
						if fmt == 'json' and callback and self.allow_callback:
							resp = '%s(%s)' % (callback, resp)
				# TODO should be date from the cache
				# headers.add('Last-Modified', http_date())
                # headers.add('Content-Length', len(resp))
			else:
				status = 200
				info = ImgInfo(img_path, ident)
				resp = info.marshal(to=fmt)


				if not os.path.exists(cache_dir): 
					os.makedirs(cache_dir, 0755)

				self.loggr.debug('made ' + cache_dir)

				if self.enable_cache:
					f = open(cache_path, 'w')
					f.write(resp)
					f.close()
					self.loggr.info('Created: ' + cache_path)

				# add the callback after we've cached
				if fmt == 'json' and callback  and self.allow_callback:
					resp = '%s(%s)' % (callback, resp)

				headers.add('Last-Modified', http_date())
				headers.add('Content-Length', len(resp))

		except LorisException, e:
			mime = 'text/xml'
			status = e.http_status
			resp = e.to_xml()
			self.loggr.info(e.message)

		except Exception, e:
			# should be safe to assume it's the server's fault.
			self.loggr.exception(e.message)
			le = LorisException(500, '', e.message)
			mime = 'text/xml'
			status = le.http_status
			resp = le.to_xml()

		finally:
			return Response(resp, status=status, content_type=mime, 
				headers=headers)

	def on_get_img(self, request, ident, region, size, rotation, quality, 
			fmt=None):
		"""Get an image.

		Most of the arguments are *Parameter objects, returned by the 
		converters.

		See <http://www-sul.stanford.edu/iiif/image-api/#parameters>

		Args:
			request (Request): The client's request.
			ident (str): The identifier for the image.
			region (RegionParameter): Internal representation of the region
				portion of an IIIF request.
			size (SizeParameter): Internal representation of the size
				portion of an IIIF request.
			rotation (RotationParameter): Internal representation of the 
				rotation portion of an IIIF request.
			quality (str): 'native', 'color', 'grey', 'bitonal'

		Kwargs:
			format (str): 'jpg' or 'png'. Default is None, in which case we 
			look first at the Accept header, and then the default format set in
			`loris.conf`.

		Returns:
			Response. Either an image, None if 304, or XML in the case of an 
			error, per IIIF 6.2
			<http://www-sul.stanford.edu/iiif/image-api/#error>
		"""
		resp = None
		status = None
		mime = None
		headers = Headers()
		headers.add('Link', self._link_hdr)
		headers.add('Cache-Control', 'public')

		if fmt == 'jpg':	
			mime = 'image/jpeg'
		elif fmt == 'png': 
			mime = 'image/png'
		elif request.headers.get('accept') == 'image/jpeg':
			fmt = 'jpg'
			mime = 'image/jpeg'
		elif request.headers.get('accept') == 'image/png':
			fmt = 'png'
			mime = 'image/png'
		else: #fmt is None 
			fmt = self.default_format
			mime = 'image/jpeg' if fmt == 'jpg' else 'image/png'

		cache_path_elements = (CACHE, ident, region, size, rotation)
		img_dir = os.sep.join(map(str, cache_path_elements))
		img_path = os.path.join(img_dir, quality + '.' + fmt)
		self.loggr.debug('img_dir: ' + img_dir)
		self.loggr.debug('img_path: ' + img_path)
	
		# check the cache
		if  self.enable_cache == True and os.path.exists(img_path):
			status = self._check_cache(img_path, request, headers)
			resp = file(img_path) if status == 200 else None
		else:
			try:
				if not os.path.exists(img_dir):	os.makedirs(img_dir, 0755)
				self.loggr.info('Made directory: ' + img_dir)
				
				self._derive_img_from_jp2(ident, img_path, region, size, 
					rotation, quality, fmt)

				status = 200
				headers.add('Content-Length', os.path.getsize(img_path))
				headers.add('Last-Modified', http_date()) # now
				resp = file(img_path)
			except LorisException, e:
				self.loggr.info(e.message)
				headers.remove('Last-Modified')
				mime = 'text/xml'
				status = e.http_status
				resp = e.to_xml()

		return Response(resp, status=status, content_type=mime, headers=headers, 
			direct_passthrough=True)

	def _check_cache(self, resource_path, request, headers):
		"""Check the cache for a resource

		Updates the headers object that we're passing a reference to, and 
		return the HTTP status that should be returned.

		Args:
			resource_path (str): Path to the file on the file system.
			request (Request): The client's request.
			headers (Headers): The headers object that will ultimately be 
				returned with the request.

		Returns:
			int. The HTTP status.
		"""
		last_change = datetime.utcfromtimestamp(os.path.getctime(resource_path))
		ims_hdr = request.headers.get('If-Modified-Since')
		ims = parse_date(ims_hdr)
		if (ims and ims > last_change) or not ims:
			status = 200
			# resp = file(img_path)
			length = length = os.path.getsize(resource_path) 
			headers.add('Content-Length', length)
			headers.add('Last-Modified', http_date(last_change))
			self.loggr.info('Read: ' + resource_path)
		else:
			status = 304
			self.loggr.info('Sent 304 for: ' + resource_path)
			headers.remove('Content-Type')
			headers.remove('Cache-Control')
		return status

	def _derive_img_from_jp2(self, ident, out_path, region, size, rotation, 
			quality, fmt, info=None):
		"""Make an image from a JP2.

		Most of the arguments are *Parameter objects, returned by the 
		converters. This is where we build and excute our shell outs.

		See <http://www-sul.stanford.edu/iiif/image-api/#parameters>

		Args:
			ident (str): The identifier for the image.
			out_path (str): The where to save the image.
			region (RegionParameter): Internal representation of the region
				portion of an IIIF request.
			size (SizeParameter): Internal representation of the size
				portion of an IIIF request.
			rotation (RotationParameter): Internal representation of the 
				rotation portion of an IIIF request.
			quality (str): 'native', 'color', 'grey', 'bitonal'
			format (str): 'jpg' or 'png'.

		Kwargs:
			info (ImgInfo): Default is None, in which case we'll read it in 
				here, but since some requests may have already read this in
				elsewhere, earlier in the in the pipe, it can be passed in to 
				avoid a second read.

		Returns:
			0 if all is good.

		Raises:
			LorisException, with a status=500 if anything goes wrong.
		"""
		try:
			fifo_path = ''
			jp2 = self._resolve_identifier(ident)
			# TODO: move the raise below into the resolve method
			if not os.path.exists(jp2):
				msg = 'Identifier does not resolve to an image.'
				raise LorisException(404, ident, msg)

			info = self._get_img_info(ident) if not info else info
		
			# Do some checking early to avoid starting to build the shell 
			# outs
			if quality not in info.qualities:
				msg = 'This quality is not available for this image.'
				raise LorisException(400, quality, msg)

			if self.cache_px_only and region.mode == 'pct':
				top_px = int(round(Decimal(region.y) * Decimal(info.height) / Decimal(100.0)))
				self.loggr.debug('top_px: ' + str(top_px))
				left_px = int(round(Decimal(region.x) * info.width / Decimal(100.0)))
				self.loggr.debug('left_px: ' + str(left_px))
				height_px = int(round(Decimal(region.h) * info.height / Decimal(100.0)))
				self.loggr.debug('height_px: ' + str(height_px))
				width_px = int(round(Decimal(region.w) * info.width / Decimal(100.0)))
				self.loggr.debug('width_px: ' + str(width_px))
				new_uri_value = ','.join(map(str, (left_px, top_px, width_px, height_px)))
				new_region_param = RegionParameter(new_uri_value)
				self.loggr.info('pct region request revised to ' + new_uri_value)
				region_kdu_arg = new_region_param.to_kdu_arg(info)
			else:
				region_kdu_arg = region.to_kdu_arg(info)
			

			# Start building and executing commands.
			# This could get a lot more sophisticated, jp2 levels for 
			# certain sizes, different utils for different formats, etc.

			# Make a named pipe for the temporary bitmap
			bmp_name = self._random_str(10) + '.bmp'
			fifo_path = os.path.join(TMP, bmp_name)
			mkfifo_call = self.mkfifo_cmd + ' ' + fifo_path
			
			self.loggr.debug('Calling ' + mkfifo_call)
			subprocess.check_call(mkfifo_call, shell=True)
			self.loggr.debug('Done (' + mkfifo_call + ')')

			# Make and call the kdu_expand cmd
			kdu_expand_call = ''
			kdu_expand_call += self.kdu_expand_cmd + ' -quiet '
			kdu_expand_call += '-i ' + jp2 
			kdu_expand_call += ' -o ' + fifo_path
			kdu_expand_call += ' ' + region_kdu_arg
			
			self.loggr.debug('Calling ' + kdu_expand_call)
			kdu_expand_proc = subprocess.Popen(kdu_expand_call, 
				shell=True, 
				bufsize=-1, 
				stderr=subprocess.PIPE,
				env=self.env)

			# make and call the convert command

			convert_call = ''
			convert_call = self.convert_cmd + ' '
			convert_call += fifo_path + ' '
			convert_call += size.to_convert_arg() + ' '
			convert_call += rotation.to_convert_arg() + ' '

			if fmt == 'jpg':
				convert_call += '-quality 90 '
			if fmt == 'png':
				convert_call += '-colors 256 -quality 00 ' 

			if quality == 'grey' and info.native_quality != 'grey':
				convert_call += '-colorspace gray -depth 8 '
			elif quality == 'bitonal':
				convert_call += '-colorspace gray -depth 1 '

			convert_call += out_path
			
			self.loggr.debug('Calling ' + convert_call)
			convert_proc = subprocess.Popen(convert_call,
				shell=True,
				bufsize=-1,
				stderr=subprocess.PIPE)
			
			convert_exit = convert_proc.wait()
			if convert_exit != 0:
				msg = '. '.join(convert_proc.stderr)
				raise LorisException(500, '', msg)
			self.loggr.debug('Done (' + convert_call + ')')
			
			kdu_exit = kdu_expand_proc.wait()
			if kdu_exit != 0:
				msg = ''
				for line in kdu_expand_proc.stderr:
					msg += line + '. '

				raise LorisException(500, '', msg)

			self.loggr.debug('Terminated ' + kdu_expand_call)
			self.loggr.info("Created: " + out_path)

			return 0
		except LorisException:
			raise
		except Exception, e:
			self.loggr.exception(e.message)
			raise LorisException(500, '', e.message)
		finally:
			# Make and call rm $fifo
			if os.path.exists(fifo_path):
				rm_fifo_call = self.rm_cmd + ' ' + fifo_path
				subprocess.call(rm_fifo_call, shell=True)

	def _get_img_info(self, ident):
		"""Gets the info from an image.

		Use this rather than directly instantiating ImgInfo objects. Here we 
		start with the memory cache, then move on to the file system cache, and
		pull from the jp2 as a last resort.

		Args:
			ident (str): The identifier for the image.

		Returns:
			ImgInfo.

		Raises:
			LorisException. If the ident does not resolve to an image.
		"""

		cache_dir = os.path.join(CACHE, ident)
		cache_path = os.path.join(cache_dir, 'info.json')

		info = None
		if self.enable_info_cache:
			info = self.info_cache.get(ident)
			
		if info is None:
			if os.path.exists(cache_path):
				info = ImgInfo(cache_path, ident)
				self.info_cache[ident] = info
			else:

				jp2 = self._resolve_identifier(ident)

				if not os.path.exists(jp2):
					msg = 'Identifier does not resolve to an image.'
					raise LorisException(404, ident, msg)
				info = ImgInfo(jp2, ident)
				
				self.info_cache[ident] = info

				if self.enable_cache:
					if not os.path.exists(cache_dir): 
						os.makedirs(cache_dir, 0755)
					f = open(cache_path, 'w')
					f.write(info.marshal('json'))
					f.close()
					self.loggr.info('Created: ' + cache_path)
		
		return info


	def _resolve_identifier(self, ident):
		"""Wraps the `resolve` function from the `resolver` module.

		Args:
			ident (str): The identifier for the image.

		Returns:
			str. The path to a JP2.
		"""
		return resolver.resolve(ident)

	def _random_str(self, size):
		"""Generates a random str of `size` length to help keep our fifos 
		unique.
		"""
		chars = ascii_lowercase + digits
		return ''.join(choice(chars) for x in range(size))

	def wsgi_app(self, environ, start_response):
		request = Request(environ)
		response = self._dispatch_request(request)
		return response(environ, start_response)

	def __call__(self, environ, start_response):
		return self.wsgi_app(environ, start_response)




if __name__ == '__main__':
	# Run the development server
	from werkzeug.serving import run_simple
	try:
		app = create_app(test=True)
		cwd = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
		extra_files = []
		extra_files.append(os.path.join(cwd, 'loris.conf'))
		extra_files.append(os.path.join(cwd, 'www', 'index.html'))
		run_simple('127.0.0.1', 5004, app, use_debugger=True, 
			threaded=True,  use_reloader=True, extra_files=extra_files)
	except Exception, e:
		sys.stderr.write(e.message)
		sys.exit(1)
