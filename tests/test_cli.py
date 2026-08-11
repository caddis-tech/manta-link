"""Nothing but a bare start may reach the port.

This process seizes the Pico's serial port as its first real act, so an argument
it does not understand has to stop it before that happens rather than after.
"""

import pytest

from manta_link import __main__, __version__


class TestArgumentsThatShouldNotStartTheDaemon:
    def test_help_prints_usage_and_exits_cleanly(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            __main__.parse_args(["--help"])

        assert exit_info.value.code == 0
        assert "usage: manta_link" in capsys.readouterr().out

    @pytest.mark.parametrize("name", __main__.CONFIG_ENV_NAMES)
    def test_help_names_every_configuration_variable(self, capsys, name):
        # There are no flags, so help is worthless unless it says where the
        # settings actually live. Parametrized over the tuple --help is built
        # from, so a new knob is covered the moment it exists rather than
        # whenever someone remembers to extend a hardcoded pair.
        with pytest.raises(SystemExit):
            __main__.parse_args(["--help"])

        assert name in capsys.readouterr().out

    def test_version_reports_the_package_version(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            __main__.parse_args(["--version"])

        assert exit_info.value.code == 0
        assert __version__ in capsys.readouterr().out

    def test_an_unrecognised_flag_is_refused_rather_than_ignored(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            __main__.parse_args(["--dry-run"])

        assert exit_info.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err

    def test_a_stray_positional_is_refused_too(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            __main__.parse_args(["/dev/ttyACM0"])

        assert exit_info.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err


class TestTheBareStart:
    def test_no_arguments_is_accepted(self):
        assert __main__.parse_args([]) is None

    def test_parsing_happens_before_anything_opens(self, monkeypatch):
        """A refused argument must not reach logging, the spool, or the port."""
        def fail(*args, **kwargs):
            raise AssertionError("startup ran despite a bad argument")

        monkeypatch.setattr(__main__.logging_setup, "configure", fail)
        monkeypatch.setattr(__main__, "install_signal_handlers", fail)
        monkeypatch.setattr(__main__, "build_recorder", fail)

        with pytest.raises(SystemExit) as exit_info:
            __main__.main(["--dry-run"])

        assert exit_info.value.code == 2
