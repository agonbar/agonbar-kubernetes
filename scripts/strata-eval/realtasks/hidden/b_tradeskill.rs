
// ---- hidden test appended by realtasks/verify.py
#[test]
fn rthidden_num_made_falls_back_to_one_one() {
    let mut s = UiScript::new().unwrap();
    s.set_trade_skill(Some(state()));
    assert_eq!(s.eval::<(i64, i64)>("return GetTradeSkillNumMade(1)").unwrap(), (1, 1), "header row");
    assert_eq!(s.eval::<(i64, i64)>("return GetTradeSkillNumMade(99)").unwrap(), (1, 1), "past the rows");
    let s2 = UiScript::new().unwrap();
    assert_eq!(s2.eval::<(i64, i64)>("return GetTradeSkillNumMade(1)").unwrap(), (1, 1), "no window");
}
