package PVE::Tools; use strict; use Exporter 'import';
our @EXPORT_OK=qw(file_get_contents file_set_contents run_command);
sub file_get_contents { my ($f)=@_; open my $fh,'<',$f or die "$f: $!"; local $/; <$fh> }
sub file_set_contents { die "not in harness" } sub run_command { die "not in harness" } 1;
