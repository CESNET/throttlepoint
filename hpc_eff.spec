%define name hpc_eff

Name:           %{name}
Version:        %{version}
Release:        %{release}
Summary:        Energy Optimization Governor
License:        BSD-3-Clause
Group:          System Environment/Base
BuildArch:      noarch
Vendor:         CESNET
Source0:        %{name}-%{version}.tar.gz

Requires:       python3
Requires:       python3-numpy
Requires:       python3-requests
BuildRequires:  ipmitool
Requires:       ipmitool
# kernel-tools provides cpupower, used to set the CPU max frequency.
Requires:       kernel-tools

# Disable .pyc/.pyo bytecode compilation during build
%define __brp_python_bytecompile %{nil}

%description
This tool dynamically optimizes energy consumption on HPC systems by
adjusting the CPU max frequency according to electricity price, carbon
intensity (CI) and/or CPU/inlet temperature. The active regulators are
selected via configuration (price/CO2, temperature, or both).

Designed to be run periodically (e.g., via cron every few minutes under
root), it classifies the current conditions, applies a frequency policy,
and records each evaluation to an SQLite database and a JSON state file.

%prep
%setup -q -n %{name}-%{version}

%build
python3 setup.py build

%install
python3 setup.py install --root=%{buildroot} --prefix=/usr --skip-build --record=INSTALLED_FILES
install -D -m 644 src/hpc_eff/config.ini.example %{buildroot}/etc/hpc_eff/config.ini
install -d %{buildroot}%{_docdir}/%{name}/examples/plugins
install -m 755 examples/plugins/*.py %{buildroot}%{_docdir}/%{name}/examples/plugins/

# The setuptools console_scripts launcher resolves its entry point at runtime
# via importlib.metadata. On Python 3.9 that reader does not reliably parse the
# legacy .egg-info this build produces, so the generated /usr/bin/hpc-eff fails
# with StopIteration. Replace it with a thin wrapper that invokes the module
# directly -- importing hpc_eff.main needs no metadata lookup.
#
# TODO: the proper fix is a wheel-based pip install (modern .dist-info reads
# reliably on every Python). It needs pip + wheel present at build time, which
# this box lacks, so do it on a hermetic builder and drop this wrapper.
cat > %{buildroot}/usr/bin/hpc-eff <<'EOF'
#!/usr/bin/python3
from hpc_eff.main import main
if __name__ == "__main__":
    main()
EOF
chmod 755 %{buildroot}/usr/bin/hpc-eff

%files -f INSTALLED_FILES
%defattr(-,root,root,-)
%config(noreplace) /etc/hpc_eff/config.ini
%doc README.md
%{_docdir}/%{name}/examples
