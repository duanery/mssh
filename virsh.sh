# virsh() -- source this file from an interactive bash (e.g. bashrc);
# if pty-bridge is not in PATH, console runs as the plain command too.
#
# Wrap "virsh ... console ..." in pty-bridge: the serial console gets the
# startup wake, the TERM/window-size sync at the prompt, and clean line
# endings when stdout is piped. Everything else runs as the plain command.
# The scan finds the first non-option word: virsh's global options are
# -c/-d/-e/-k/-K/-l (value) and -h/-q/-r/-t/-v/-V/--no-pkttyagent
# (flags), long forms included,
# with values attached (--connect=URI) or separate (--connect URI), short
# options grouped (-qrt) or with the value attached (-d1).
#
# pty-bridge execs a fresh child; /usr/bin/command PATH-executes the
# real virsh there and skips shell functions by definition, so this
# wrapper cannot re-trigger -- `command` in the other branch bypasses
# it the same way.
virsh() {
    local -a args=("$@")
    local i=0 cmd="" a c rest

    while [ $i -lt ${#args[@]} ]; do
        a=${args[$i]}
        case $a in
            --) # end of options: the next argument, if any, is the command
                i=$((i + 1))
                [ $i -lt ${#args[@]} ] && cmd=${args[$i]}
                break ;;
            --c*|--d*|--e*|--k*|--l*)
                # long option taking a value (--connect, --debug,
                # --escape, --keepalive-interval, --keepalive-count,
                # --log, unambiguous prefixes included);
                # --connect=URI carries it, --connect URI takes the next
                case $a in *=*) ;; *) i=$((i + 1)) ;; esac ;;
            --*) ;; # long flag: --quiet, --readonly, --help, ...
            -*)   # short options: walk the group, stop at the first one
                  # taking a value -- the rest of the group is attached,
                  # or the next argument when the group ends there
                rest=${a#-}
                while [ -n "$rest" ]; do
                    c=${rest:0:1}
                    rest=${rest:1}
                    case $c in
                        [cdelkK])       # -c -d -e -k -K -l take a value
                            [ -n "$rest" ] || i=$((i + 1))
                            break ;;
                    esac
                done ;;
            *)    # first non-option: the virsh command word
                cmd=$a
                break ;;
        esac
        i=$((i + 1))
    done

    if [ "$cmd" = console ] && command -v pty-bridge >/dev/null 2>&1; then
        pty-bridge -p ']# ' --term serial -- command virsh "$@"
    else
        command virsh "$@"
    fi
}
