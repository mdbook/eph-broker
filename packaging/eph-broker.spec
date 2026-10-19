%global pkg_version %{?pkg_version}%{!?pkg_version:0.0.0}
%global pkg_release %{?pkg_release}%{!?pkg_release:0}
%global commit %{?commit}%{!?commit:unknown}

Name:           eph-broker
Version:        %{pkg_version}
Release:        %{pkg_release}%{?dist}
Summary:        Ephemeral coding-agent worker broker (127.0.0.1:8787)

License:        GPL-3.0-only
URL:             https://github.com/mdbook/eph-broker
Source0:        %{name}-%{pkg_version}.tar.gz

BuildArch:      noarch
BuildRequires:  systemd-rpm-macros

Requires:       python3 >= 3.9
Requires:       git
Requires(pre):  shadow-utils
# only the interactive-agent row and its duplicate check use tmux
Recommends:     tmux
# only for a YAML config/broker.yaml; a .json BROKER_CONFIG needs nothing extra
Recommends:     python3-pyyaml
%{?systemd_requires}

%description
eph-broker turns tracker tickets and GitLab issues into short-lived coding
agent sessions (Claude Code by default), each in its own git worktree, and
hands back a merge request for a read-only reviewer. It batches and routes
inbound events and serves a dashboard. Pure Python stdlib, no venv.

Built from commit %{commit}.

%prep
%autosetup -n %{name}-%{pkg_version}

%build
# nothing to compile: it's a pure-Python app

%install
install -d -m 0755 %{buildroot}/opt/eph-broker
cp -a *.py %{buildroot}/opt/eph-broker/
cp -a adapters %{buildroot}/opt/eph-broker/
cp -a trackers %{buildroot}/opt/eph-broker/
cp -a config %{buildroot}/opt/eph-broker/
cp -a modules %{buildroot}/opt/eph-broker/
cp -a prompts %{buildroot}/opt/eph-broker/
cp -a workers %{buildroot}/opt/eph-broker/
cp -a tools %{buildroot}/opt/eph-broker/
cp -a approved-prompts.json %{buildroot}/opt/eph-broker/

install -d -m 0755 %{buildroot}%{_unitdir}
install -m 0644 packaging/eph-broker.service %{buildroot}%{_unitdir}/eph-broker.service
install -d -m 0755 %{buildroot}%{_unitdir}/eph-broker.service.d

%files
%license LICENSE
%dir /opt/eph-broker
/opt/eph-broker/*
%{_unitdir}/eph-broker.service
%dir %{_unitdir}/eph-broker.service.d

%pre
getent group eph-broker >/dev/null || groupadd -r eph-broker
getent passwd eph-broker >/dev/null || \
    useradd -r -g eph-broker -M -d /var/lib/eph-broker -s /sbin/nologin -c "eph-broker service" eph-broker
exit 0

%post
%systemd_post eph-broker.service

%preun
%systemd_preun eph-broker.service

%postun
%systemd_postun_with_restart eph-broker.service

%changelog
* Fri Sep 25 2026 eph-broker CI <ci@eph-broker.invalid> - %{pkg_version}-%{pkg_release}
- CI-built package (commit %{commit})
