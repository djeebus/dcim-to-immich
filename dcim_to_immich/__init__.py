import gi

# Pin GTK 3 before anything imports gi.repository.Gtk (GTK 4 may also be installed).
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
