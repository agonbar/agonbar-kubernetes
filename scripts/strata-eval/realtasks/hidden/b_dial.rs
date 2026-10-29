
// ---- hidden tests appended by realtasks/verify.py
#[cfg(test)]
mod rthidden_dial_tests {
    use super::{dial, DialFailure};
    use std::net::TcpListener;

    #[test]
    fn rthidden_localhost_dials_ipv4() {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let s = dial("localhost", port).expect("localhost must reach a listener on 127.0.0.1");
        assert!(s.peer_addr().unwrap().is_ipv4());
    }

    #[test]
    fn rthidden_never_dials_ipv6() {
        // A listener on ::1 only: 1.12.1 never dials IPv6, so localhost must not reach it.
        let Ok(l) = TcpListener::bind("[::1]:0") else { eprintln!("no ::1 here, skipped"); return };
        let port = l.local_addr().unwrap().port();
        assert!(dial("localhost", port).is_err(), "dialled ::1");
    }

    #[test]
    fn rthidden_ipv6_only_host_is_unresolved() {
        let e = dial("::1", 1).expect_err("an IPv6-only host must fail");
        let f = e.downcast_ref::<DialFailure>().expect("a DialFailure");
        assert!(f.unresolved, "must fail as unresolved, not as a refused dial");
    }
}
