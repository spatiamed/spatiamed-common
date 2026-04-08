from sm_common.nmc_guardrails import check_content, has_blocking_violation


class TestCheckContent:
    def test_compliant_text(self):
        violations = check_content("Our experienced doctors provide quality care.")
        assert violations == []

    def test_experienced_doctor_passes(self):
        """Critical contract: 'experienced doctor' must pass."""
        violations = check_content("Consult our experienced doctor for treatment options.")
        assert violations == []

    def test_best_doctor_blocked(self):
        """Critical contract: 'best doctor' must be blocked."""
        violations = check_content("Visit the best doctor in town!")
        assert len(violations) >= 1
        assert any(v.rule == "superlative_claims" for v in violations)
        assert has_blocking_violation(violations) is True

    def test_guaranteed_cure_blocked(self):
        violations = check_content("We guarantee a cure for your condition.")
        assert any(v.rule == "guaranteed_outcomes" for v in violations)
        assert has_blocking_violation(violations) is True

    def test_100_percent_blocked(self):
        violations = check_content("100% success rate!")
        assert any(v.rule == "guaranteed_outcomes" for v in violations)

    def test_testimonial_blocked(self):
        violations = check_content("Read our success stories from patients.")
        assert any(v.rule == "misleading_testimonials" for v in violations)

    def test_free_consultation_warned(self):
        violations = check_content("Book a free consultation today!")
        assert any(v.rule == "pricing_inducement" for v in violations)
        # pricing_inducement is warn, not block
        pricing_violations = [v for v in violations if v.rule == "pricing_inducement"]
        assert all(v.severity == "warn" for v in pricing_violations)

    def test_discount_surgery_warned(self):
        violations = check_content("Get a discount on surgery this month!")
        assert any(v.rule == "pricing_inducement" for v in violations)

    def test_world_class_blocked(self):
        violations = check_content("World-class medical facilities.")
        assert any(v.rule == "superlative_claims" for v in violations)

    def test_leading_hospital_blocked(self):
        violations = check_content("The leading hospital in the region.")
        assert any(v.rule == "superlative_claims" for v in violations)

    def test_case_insensitive(self):
        violations = check_content("THE BEST DOCTOR IN TOWN")
        assert len(violations) >= 1

    def test_multiple_violations(self):
        violations = check_content("The best doctor guarantees a cure!")
        assert len(violations) >= 2


class TestHasBlockingViolation:
    def test_empty_list(self):
        assert has_blocking_violation([]) is False

    def test_only_warnings(self):
        violations = check_content("Book a free consultation today!")
        # Only pricing_inducement (warn severity)
        non_block = [v for v in violations if v.severity != "block"]
        assert has_blocking_violation(non_block) is False

    def test_with_block(self):
        violations = check_content("We are the best hospital.")
        assert has_blocking_violation(violations) is True
