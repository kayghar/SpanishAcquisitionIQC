import logging
log = logging.getLogger(__name__)


name = 'Stanford Research Systems'

from . import sr830dsp
models = [sr830dsp]
log.debug('Found models for "{0}": {1}'.format(name, ''.join(str(x) for x in models)))

from .mock import mock_sr830dsp
mock_models = [mock_sr830dsp]
log.debug('Found mock models for "{0}": {1}'.format(name, ''.join(str(x) for x in mock_models)))
