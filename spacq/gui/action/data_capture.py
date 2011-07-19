import csv
from datetime import timedelta
from functools import partial
import os
from pubsub import pub
from threading import Lock, Thread
from time import localtime, sleep, time
import wx
from wx.lib.filebrowsebutton import DirBrowseButton

from spacq.iteration.sweep import SweepController
from spacq.iteration.variables import sort_variables, InputVariable, OutputVariable
from spacq.tool.box import flatten, sift

from ..tool.box import Dialog, MessageDialog, YesNoQuestionDialog


class DataCaptureDialog(Dialog, SweepController):
	"""
	A progress dialog which runs over iterators, sets the corresponding resources, and captures the measured data.
	"""

	timer_delay = 50 # ms
	stall_time = 2 # s

	status_messages = {
		None: 'Starting up',
		'init': 'Initializing',
		'next': 'Getting next values',
		'transition': 'Smooth setting',
		'write': 'Writing to devices',
		'dwell': 'Waiting for devices to settle',
		'read': 'Taking measurements',
		'ramp_down': 'Smooth setting',
		'end': 'Finishing',
	}

	def __init__(self, parent, resources, variables, num_items, measurement_resources,
			measurement_variables, continuous=False, *args, **kwargs):
		kwargs['style'] = kwargs.get('style', wx.DEFAULT_DIALOG_STYLE) | wx.RESIZE_BORDER

		Dialog.__init__(self, parent, title='Sweeping...', *args, **kwargs)
		SweepController.__init__(self, resources, variables, num_items, measurement_resources,
				measurement_variables, continuous=continuous)

		self.parent = parent

		# Show only elapsed time in continuous mode.
		self.show_remaining_time = not self.continuous

		self.last_checked_time = -1
		self.elapsed_time = 0 # us

		self.timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self.OnTimer, self.timer)

		self.cancelling = False

		def write_callback(pos, i, value):
			self.value_outputs[pos][i].Value = str(value)
		self.write_callback = partial(wx.CallAfter, write_callback)

		def read_callback(i, value):
			self.value_inputs[i].Value = str(value)
		self.read_callback = partial(wx.CallAfter, read_callback)

		# Dialog.
		dialog_box = wx.BoxSizer(wx.VERTICAL)

		## Progress.
		progress_box = wx.BoxSizer(wx.HORIZONTAL)
		dialog_box.Add(progress_box, flag=wx.EXPAND|wx.ALL, border=5)

		### Message.
		self.progress_percent = wx.StaticText(self, label='', size=(40, -1))
		progress_box.Add(self.progress_percent,
				flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.ALL, border=5)

		### Bar.
		self.progress_bar = wx.Gauge(self, range=num_items, style=wx.GA_HORIZONTAL)
		progress_box.Add(self.progress_bar, proportion=1)

		## Status.
		self.status_message_output = wx.TextCtrl(self, style=wx.TE_READONLY)
		self.status_message_output.BackgroundColour = wx.LIGHT_GREY
		dialog_box.Add(self.status_message_output, flag=wx.EXPAND)

		## Values.
		self.values_box = wx.FlexGridSizer(rows=len(self.variables), cols=2, hgap=20)
		self.values_box.AddGrowableCol(1, 1)
		dialog_box.Add(self.values_box, flag=wx.EXPAND|wx.ALL, border=5)

		self.value_outputs = []
		for group in self.variables:
			group_outputs = []

			for var in group:
				output = wx.TextCtrl(self, style=wx.TE_READONLY)
				output.BackgroundColour = wx.LIGHT_GREY
				group_outputs.append(output)

				self.values_box.Add(wx.StaticText(self, label=var.name),
						flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT)
				self.values_box.Add(output, flag=wx.EXPAND)

			self.value_outputs.append(group_outputs)


		for _ in xrange(2):
			self.values_box.Add(wx.StaticLine(self), flag=wx.EXPAND|wx.ALL, border=5)

		self.value_inputs = []
		for var in self.measurement_variables:
			input = wx.TextCtrl(self, style=wx.TE_READONLY)
			input.BackgroundColour = wx.LIGHT_GREY
			self.value_inputs.append(input)

			self.values_box.Add(wx.StaticText(self, label=var.name),
					flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT)
			self.values_box.Add(input, flag=wx.EXPAND)

		## Times.
		times_box = wx.FlexGridSizer(rows=2 if self.show_remaining_time else 1, cols=2, hgap=5)
		dialog_box.Add(times_box, proportion=1, flag=wx.CENTER|wx.ALL, border=15)

		### Elapsed.
		times_box.Add(wx.StaticText(self, label='Elapsed time:'))
		self.elapsed_time_output = wx.StaticText(self, label='---:--:--')
		times_box.Add(self.elapsed_time_output)

		### Remaining.
		if self.show_remaining_time:
			times_box.Add(wx.StaticText(self, label='Remaining time:'))
			self.remaining_time_output = wx.StaticText(self, label='---:--:--')
			times_box.Add(self.remaining_time_output)

		## End button.
		button_box = wx.BoxSizer(wx.HORIZONTAL)
		dialog_box.Add(button_box, flag=wx.CENTER)

		self.cancel_button = wx.Button(self, label='Cancel')
		self.Bind(wx.EVT_BUTTON, self.OnCancel, self.cancel_button)
		button_box.Add(self.cancel_button)

		self.SetSizerAndFit(dialog_box)

		# Try to cancel cleanly instead of giving up.
		self.Bind(wx.EVT_CLOSE, self.OnCancel)

	def resource_exception_handler(self, resource_name, e, write=True):
		"""
		Called when a write to or read from a Resource raises e.
		"""

		msg = 'Resource: {0}\nError: {1}'.format(resource_name, str(e))
		dir = 'to' if write else 'from'
		MessageDialog(self.parent, msg, 'Error writing {0} resource'.format(dir)).Show()

		self.abort(fatal=write)

	def start(self):
		thr = Thread(target=SweepController.run, args=(self,))
		thr.daemon = True
		thr.start()

		self.timer.Start(self.timer_delay)

	def end(self):
		try:
			SweepController.end(self)
		except AssertionError:
			return

		# In case the sweep is too fast, ensure that the user has some time to see the dialog.
		span = time() - self.sweep_start_time
		if span < self.stall_time:
			sleep(self.stall_time - span)

		wx.CallAfter(self.timer.Stop)
		wx.CallAfter(self.Destroy)

	def OnCancel(self, evt=None):
		if not self.cancel_button.Enabled:
			return

		self.cancel_button.Disable()
		self.cancelling = True

	def OnTimer(self, evt=None):
		# Update status.
		self.status_message_output.Value = self.status_messages[self.current_f]

		# Update progress.
		if self.num_items > 0 and self.item >= 0:
			amount_done = float(self.item) / self.num_items

			self.progress_bar.Value = self.item
			self.progress_percent.Label = '{0}%'.format(int(100 * amount_done))

			if self.last_checked_time > 0:
				self.elapsed_time += int((time() - self.last_checked_time) * 1e6)
				self.elapsed_time_output.Label = str(timedelta(seconds=self.elapsed_time//1e6))

			self.last_checked_time = time()

			if self.show_remaining_time and amount_done > 0:
				total_time = self.elapsed_time / amount_done
				remaining_time = int(total_time - self.elapsed_time)
				self.remaining_time_output.Label = str(timedelta(seconds=remaining_time//1e6))

		# Prompt to abort.
		if self.cancelling:
			def abort():
				self.cancelling = False

				thr = Thread(target=self.abort)
				thr.daemon = True
				thr.start()

				self.timer.Start(self.timer_delay)

			def resume():
				self.cancelling = False

				with self.pause_lock:
					self.paused = False
					self.pause_lock.notify()

				self.cancel_button.Enable()

				self.timer.Start(self.timer_delay)

			self.paused = True

			self.last_checked_time = -1
			self.timer.Stop()

			YesNoQuestionDialog(self, 'Abort processing?', abort, resume).Show()

			return


class DataCapturePanel(wx.Panel):
	def __init__(self, parent, global_store, *args, **kwargs):
		wx.Panel.__init__(self, parent, *args, **kwargs)

		self.global_store = global_store

		self.capture_dialogs = 0

		# Panel.
		panel_box = wx.BoxSizer(wx.HORIZONTAL)

		## Capture.
		capture_static_box = wx.StaticBox(self, label='Capture')
		capture_box = wx.StaticBoxSizer(capture_static_box, wx.VERTICAL)
		panel_box.Add(capture_box, flag=wx.CENTER|wx.ALL, border=5)

		### Start.
		self.start_button = wx.Button(self, label='Start')
		self.Bind(wx.EVT_BUTTON, self.OnBeginCapture, self.start_button)
		capture_box.Add(self.start_button, flag=wx.CENTER)

		### Continuous.
		self.continuous_checkbox = wx.CheckBox(self, label='Continuous')
		capture_box.Add(self.continuous_checkbox, flag=wx.CENTER)

		## Export.
		export_static_box = wx.StaticBox(self, label='Export')
		export_box = wx.StaticBoxSizer(export_static_box, wx.HORIZONTAL)
		panel_box.Add(export_box, proportion=1, flag=wx.CENTER|wx.ALL, border=5)

		### Enabled.
		self.export_enabled = wx.CheckBox(self, label='')
		self.export_enabled.Value = True
		export_box.Add(self.export_enabled, flag=wx.CENTER)

		### Export path.
		export_path_box = wx.BoxSizer(wx.VERTICAL)
		export_box.Add(export_path_box, proportion=1, flag=wx.CENTER)

		#### Directory.
		self.directory_browse_button = DirBrowseButton(self, labelText='Directory:')
		export_path_box.Add(self.directory_browse_button, flag=wx.EXPAND)

		#### Last file.
		last_file_box = wx.BoxSizer(wx.HORIZONTAL)
		export_path_box.Add(last_file_box, flag=wx.EXPAND)

		last_file_box.Add(wx.StaticText(self, label='Last output: '),
				flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT)
		self.last_file_name = wx.TextCtrl(self, style=wx.TE_READONLY)
		self.last_file_name.BackgroundColour = wx.LIGHT_GREY
		last_file_box.Add(self.last_file_name, proportion=1)

		self.SetSizer(panel_box)

	def OnBeginCapture(self, evt=None):
		# Prevent accidental double-clicking.
		self.start_button.Disable()
		def enable_button():
			sleep(1)
			wx.CallAfter(self.start_button.Enable)
		thr = Thread(target=enable_button)
		thr.daemon = True
		thr.start()

		all_variables = [var for var in self.global_store.variables.values() if var.enabled]
		output_variables = sift(all_variables, OutputVariable)
		input_variables = [var for var in sift(all_variables, InputVariable) if var.resource_name != '']

		if not output_variables:
			MessageDialog(self, 'No output variables defined', 'No variables').Show()
			return

		output_variables, num_items = sort_variables(output_variables)

		resource_names = [tuple(var.resource_name for var in group) for group in output_variables]
		measurement_resource_names = [var.resource_name for var in input_variables]

		continuous = self.continuous_checkbox.Value

		missing_resources = []
		unreadable_resources = []
		unwritable_resources = []

		resources = []
		for group in resource_names:
			group_resources = []

			for name in group:
				if name == '':
					group_resources.append((str(len(resources)), None))
				elif name not in self.global_store.resources:
					missing_resources.append(name)
				else:
					resource = self.global_store.resources[name]

					if resource.writable:
						group_resources.append((name, resource))
					else:
						unwritable_resources.append(name)

			resources.append(tuple(group_resources))

		measurement_resources = []
		for name in measurement_resource_names:
			if name not in self.global_store.resources:
				missing_resources.append(name)
			else:
				resource = self.global_store.resources[name]

				if resource.readable:
					measurement_resources.append((name, resource))
				else:
					unreadable_resources.append(name)

		if missing_resources:
			MessageDialog(self, ', '.join(missing_resources), 'Missing resources').Show()
		if unreadable_resources:
			MessageDialog(self, ', '.join(unreadable_resources), 'Unreadable resources').Show()
		if unwritable_resources:
			MessageDialog(self, ', '.join(unwritable_resources), 'Unwritable resources').Show()
		if missing_resources or unreadable_resources or unwritable_resources:
			return

		exporting = False
		if self.export_enabled.Value:
			dir = self.directory_browse_button.GetValue()
			# YYYY-MM-DD_HH-MM-SS.csv
			name = '{0:04}-{1:02}-{2:02}_{3:02}-{4:02}-{5:02}.csv'.format(*localtime())

			if not dir:
				MessageDialog(self, 'No directory selected.', 'Export path').Show()
				return

			if not os.path.isdir(dir):
				MessageDialog(self, 'Invalid directory selected', 'Export path').Show()
				return

			file_path = os.path.join(dir, name)
			if os.path.exists(file_path):
				MessageDialog(self, file_path, 'File exists').Show()
				return

			# Everything looks alright, so open the file.
			export_file = open(file_path, 'w')
			export_csv = csv.writer(export_file)
			exporting = True

			# Show the path in the GUI.
			self.last_file_name.Value = file_path

			# Write the header.
			export_csv.writerow(['__time__'] + [var.name for var in flatten(output_variables)] +
					[var.name for var in input_variables])

		self.capture_dialogs += 1

		dlg = DataCaptureDialog(self, resources, output_variables, num_items, measurement_resources,
				input_variables, continuous)

		for name in measurement_resource_names:
			pub.sendMessage('data_capture.start', name=name)

		# Export buffer.
		max_buf_size = 10
		buf = []
		buf_lock = Lock()

		def flush():
			export_csv.writerows(buf)
			export_file.flush()

			while buf:
				buf.pop()

		def data_callback(cur_time, values, measurement_values):
			for name, value in zip(measurement_resource_names, measurement_values):
				pub.sendMessage('data_capture.data', name=name, value=value)

			if exporting:
				with buf_lock:
					buf.append((cur_time,) + values + measurement_values)

					if len(buf) >= max_buf_size:
						flush()

		def close_callback():
			self.capture_dialogs -= 1

			if exporting:
				with buf_lock:
					flush()
					export_file.close()

			for name in measurement_resource_names:
				pub.sendMessage('data_capture.stop', name=name)

		dlg.data_callback = data_callback
		dlg.close_callback = close_callback
		dlg.Show()
		dlg.start()
