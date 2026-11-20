package PVE::RESTEnvironment; use strict; use Exporter 'import'; our @EXPORT_OK=qw(log_warn);
sub log_warn { warn "WARN: @_\n" } 1;
