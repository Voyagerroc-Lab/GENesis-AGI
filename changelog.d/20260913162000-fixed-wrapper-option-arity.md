- **Three ways of writing a command that the safety guards could not see.** The
  guards work out what a command actually runs by looking through the small
  helper programs people put in front of it — the ones that set an environment
  variable, apply a timeout, or feed arguments in from a list. To do that the
  parser has to know which of a helper's own options are followed by a value, so
  it can step over them and reach the real command. Two entries in that table
  were wrong in the direction that matters: they claimed an option takes a value
  when it only takes one optionally, so the parser stepped over the command
  itself and the guards examined its first argument instead. A third helper
  option, which carries an entire command line inside a single quoted argument,
  was not known at all, so the whole line was read as a program name. All three
  forms run, and all three reached a guard that would otherwise have refused
  them. The parser now reads the carried command line as the command it is, the
  two wrong entries are gone, and a test re-derives the table from each tool's
  own documentation on the machine it runs on, so an entry that stops matching
  reality fails the build rather than going quiet. Replayed against 68,308 real
  commands from one install's history, 1,744 of which use these helpers: no
  command's reading changed except the ones this fixes.
