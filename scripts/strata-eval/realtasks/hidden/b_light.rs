
// ---- hidden tests appended by realtasks/verify.py (from the real commit's test)
#[cfg(test)]
mod rthidden_light_tests {
    use super::segment;

    #[test]
    fn rthidden_segment_walks_unordered_keys_as_a_cycle() {
        assert_eq!(segment(&[720, 1440, 0], 1080), (0, 1, 0.5));
        assert_eq!(segment(&[720, 1440, 0], 2160), (1, 2, 0.5));
        let (i0, i1, f) = segment(&[2640, 2520, 0, 1440], 1440);
        assert_eq!((i0, i1), (0, 1));
        assert!((f - (1440.0 + 2880.0 - 2640.0) / 2760.0).abs() < 1e-6);
    }

    #[test]
    fn rthidden_segment_ascending_unchanged() {
        // Ascending keys behave as before: inside a pair, and the wrap from last to first.
        assert_eq!(segment(&[0, 1440], 720), (0, 1, 0.5));
        let (i0, i1, f) = segment(&[720, 2160], 0);
        assert_eq!((i0, i1), (1, 0));
        assert!((f - 720.0 / 1440.0).abs() < 1e-6);
    }
}
