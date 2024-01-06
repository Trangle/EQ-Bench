import subprocess
import threading
import re
import psutil
import signal
import time
import os
import pexpect
from lib.download import download_model
import sys
import shlex

class Ooba:
	def __init__(self, script_path, model_path, cache_dir, verbose, trust_remote_code=False, ooba_args_global="", 
				  ooba_args="", fast_download=False, include_patterns=None, exclude_patterns=None, hf_access_token=None,
				  load_model=True):
		self.script_path = script_path
		if script_path.endswith('sh'):
			self.script_command = 'bash'
		elif script_path.endswith('server.py'):
			self.script_command = sys.executable
		self.ooba_dir = os.path.dirname(os.path.abspath(self.script_path))
		self.model_path = model_path
		if cache_dir:
			expanded_path = os.path.expanduser(cache_dir)
			self.cache_dir = os.path.abspath(expanded_path)		
		else:
			self.cache_dir = None
		self.verbose = verbose
		self.trust_remote_code = trust_remote_code
		self.ooba_args = ooba_args
		self.ooba_args_global = ooba_args_global
		self.fast_download = fast_download
		self.include_patterns = include_patterns
		self.exclude_patterns = exclude_patterns
		self.hf_access_token = hf_access_token
		self.load_model = load_model

		self.process = None
		self.url_found_event = threading.Event()
		self.url = None
		self.output_lock = threading.Lock()
		self.full_output = ''
		self.shutdown_message_shown = threading.Event()
		self.process_end_event = threading.Event()
		self.model_downloaded_fullpath = None

	def is_already_running(self):
		# Resolve the directory of the script_path
		for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
			try:
					# Check if the process is a Python process
					if 'python' in proc.name().lower():
						# Check if server.py is in the command line
						if any('server.py' in cmd for cmd in proc.cmdline()):
							# Try to get the current working directory of the process
							process_cwd = proc.cwd()
							# Check if the process's working directory matches the script's directory
							if os.path.abspath(process_cwd) == self.ooba_dir:
								return True
			except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
					continue

		return False

	def start(self):
		if self.is_already_running():
			print('Ooobabooga server appears to already be running! Please close it before running the benchmark.')
			exit()			

		if self.load_model:
			self.download_model()
		else:
			# this path is used by quickstart.py when first launching ooba
			self.command_args = [self.script_command, self.script_path]

		if self.trust_remote_code:
			self.command_args += ['--trust-remote-code']		
		
		self.command_args += ['--api']

		if self.ooba_args:
			self.command_args += shlex.split(self.ooba_args)
		elif self.ooba_args_global:
			self.command_args += shlex.split(self.ooba_args_global)

		if self.verbose:
			print('Launching ooba with command:')
			print(' '.join(self.command_args))
		
		self.process = pexpect.spawn(self.command_args[0], self.command_args[1:], encoding='utf-8', timeout=None, cwd = self.ooba_dir)
		threading.Thread(target=self.monitor_output, daemon=True).start()

		# Wait for either URL found or process end
		while not (self.url_found_event.is_set() or self.process_end_event.is_set()):
			time.sleep(0.5)

		if self.url_found_event.is_set():
			return self.url
		else:
			print("Process ended before URL was found.")
			return False
		
	def download_model(self):
		# figure out if this is an existing local path
		if os.path.exists(self.model_path):			
			self.command_args = [self.script_command, self.script_path, '--model', self.model_path] + self.ooba_args.split()	
		else:			
			# it's not an existing local path, so try using hf_hub downloader to fetch the model
			download_path = download_model(self.model_path, self.cache_dir, self.ooba_dir, self.include_patterns, self.exclude_patterns, self.hf_access_token)

			# Check if the download path was found
			if download_path is None:
				raise Exception("Download path not found in the output.")
			else:
				print(f"Model downloaded to: {download_path}")

			self.model_downloaded_fullpath = download_path

			model_dir, model = os.path.split(download_path)

			self.command_args = [self.script_command, self.script_path, '--model', model, '--model-dir', model_dir] + self.ooba_args.split()
		
			return

	def read_output_for_duration(self, duration):
		end_time = time.time() + duration
		outstr = ''
		try:
			while time.time() < end_time:
				try:
						outstr += self.process.read_nonblocking(size=1024, timeout=0.1)						
				except pexpect.TIMEOUT:
						# Continue reading if we haven't reached the end time
						continue
				except pexpect.EOF:
						# Process ended, break from the loop
						break
		except Exception as e:
			# Handle any unexpected exceptions
			print(f"An unexpected exception occurred: {e}")
		finally:
			return outstr

	def monitor_output(self):
		#url_pattern = re.compile(r"Uvicorn running on\s*(?:\x1b\[\d+m)*(http://\S+)(?:\x1b\[\d+m)* \(Press CTRL\+C to quit\)")
		url_pattern = re.compile(r"Uvicorn running on\s*(?:\x1b\[\d+m)?(http:\/\/[^\s\x1b]+)(?:\x1b\[\d+m)?\s\(Press CTRL\+C to quit\)")
		error_pathnotfound_pattern = re.compile(r"The path to the model does not exist\. Exiting\.")
		shutdown_pattern = re.compile(r"Shutting down Text generation web UI gracefully\.")
		exception_pattern = re.compile(r"Traceback \(most recent call last\)")

		output_buffer = ""

		while True:
			try:
				# This is a hacky way to stream in the ooba progress bar updates since other methods didn't work.				
				char = self.process.read_nonblocking(size=1, timeout=None)
				output_buffer += char

				if char == '\n' or char == '\r' or output_buffer.endswith('it/s]'):
						# Print the accumulated output
						print(output_buffer, end='\r' if (char == '\r' or output_buffer.endswith('it/s]')) else '\n')						

						if not output_buffer.endswith('it/s]'):
							# Check for patterns in the output buffer						
							if url_pattern.search(output_buffer):
								self.url = url_pattern.search(output_buffer).group(1).strip()
								print(f"\nURL found: {self.url}")
								self.url_found_event.set()
								return

							if error_pathnotfound_pattern.search(output_buffer):
								print("\nError: the path to the model does not exist.")
								self.stop(force_exit=True)
								return
							
							if exception_pattern.search(output_buffer):
								# Read the rest of the exception text
								exception_str = self.read_output_for_duration(1)							
								print(exception_str)							
								print("\nError: Oobabooga failed to load the model.")
								self.stop(force_exit=True)
								return

							if shutdown_pattern.search(output_buffer):
								print("\nOobabooga is shutting down...")
								self.shutdown_message_shown.set()
								return

						# Clear the output buffer
						output_buffer = ""

			except pexpect.EOF:
				self.process_end_event.set()
				break
			except pexpect.TIMEOUT:
				# If a timeout occurs, just loop back and continue reading
				continue
			time.sleep(0.001)

	def stop(self, force_exit=False):
		if self.process is not None:
			# Send SIGINT to the process group
			try:
				os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
			except Exception as e:
				return

			if not force_exit:
				self.shutdown_message_shown.wait(timeout=5)
				# Wait an additional 2s
				time.sleep(2)

			# Terminate the process if still running
			if self.process.isalive():
				self.process.kill(signal.SIGKILL)

		self.process_end_event.set()
