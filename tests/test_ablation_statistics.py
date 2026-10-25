"""Independent, hand-calculated checks for the offline statistics API."""
import math
import unittest

from analysis import statistics


class StatisticsTests(unittest.TestCase):
    def test_ranks_average_ties_without_reordering_input(self):
        self.assertEqual(statistics.average_ranks([10, 30, 20, 30]), [4., 1.5, 3., 1.5])
        self.assertEqual(statistics.average_ranks([10, 30, 20, 30], descending=False),
                         [1., 3.5, 2., 3.5])
        self.assertEqual(statistics.average_ranks([]), [])

    def test_ranks_reject_missing_nonfinite_and_boolean_values(self):
        for value in (None, float('nan'), float('inf'), True, '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                statistics.average_ranks([1., value])

    def test_pearson_hand_calculated_nontrivial_correlation(self):
        # Centered products sum to 3; centered square sums are 2 and 14/3.
        result = statistics.pearson([1., 2., 3.], [1., 2., 4.])
        self.assertAlmostEqual(result['value'], 0.9819805060619657)
        self.assertEqual(result['n'], 3)
        self.assertIsNone(result['reason'])

    def test_pearson_near_full_score_preserves_positive_correlation(self):
        # Two distinct points always have magnitude-one correlation. The mean
        # of these adjacent floats is not itself representable near 1.0.
        result = statistics.pearson([0.9999999999999999, 1.0], [0.0, 1.0])
        self.assertAlmostEqual(result['value'], 1.0)
        self.assertEqual(result['n'], 2)
        self.assertIsNone(result['reason'])

    def test_pearson_near_full_score_preserves_negative_correlation(self):
        result = statistics.pearson([0.9999999999999999, 1.0], [1.0, 0.0])
        self.assertAlmostEqual(result['value'], -1.0)
        self.assertEqual(result['n'], 2)
        self.assertIsNone(result['reason'])

    def test_pearson_near_full_score_preserves_unequal_spacing(self):
        # Exactly representable offsets from the first observation are
        # [0, 2**-53, 4 * 2**-53], an affine copy of [0, .25, 1].
        result = statistics.pearson(
            [0.9999999999999996, 0.9999999999999997, 1.0], [0.0, 0.25, 1.0])
        self.assertAlmostEqual(result['value'], 1.0)
        self.assertEqual(result['n'], 3)

    def test_spearman_uses_average_ties(self):
        result = statistics.spearman([1., 2., 2., 4.], [1., 2., 3., 4.])
        self.assertAlmostEqual(result['value'], 0.9486832980505138)
        self.assertEqual(result['n'], 4)

    def test_kendall_uses_tau_b_tie_correction(self):
        # P=4, Q=0, x-only ties=1, y-only ties=1: tau_b = 4/5.
        result = statistics.kendall_tau_b([1, 1, 2, 3], [1, 2, 2, 3])
        self.assertAlmostEqual(result['value'], 0.8)
        self.assertEqual(result['n'], 4)

    def test_kendall_counts_discordance_and_joint_ties(self):
        self.assertAlmostEqual(
            statistics.kendall_tau_b([1, 2, 3], [3, 1, 2])['value'], -1. / 3.)
        self.assertEqual(
            statistics.kendall_tau_b([1, 1, 2], [2, 2, 1])['value'], -1.)

    def test_correlations_pairwise_filter_without_zero_imputation(self):
        x = [1., None, 3., float('inf'), 5., True, '9', float('nan')]
        y = [2., 99., 6., 20., 10., 30., 99., 80.]
        for function in (statistics.pearson, statistics.spearman, statistics.kendall_tau_b):
            with self.subTest(function=function.__name__):
                result = function(x, y)
                self.assertEqual(result['n'], 3)
                self.assertAlmostEqual(result['value'], 1.)
                self.assertIsNone(result['reason'])

    def test_insufficient_and_constant_inputs_are_explicit_nulls(self):
        for function in (statistics.pearson, statistics.spearman, statistics.kendall_tau_b):
            with self.subTest(function=function.__name__):
                self.assertEqual(function([1, None], [2, 3]),
                                 {'value': None, 'n': 1, 'reason': 'insufficient_pairs'})
                self.assertEqual(function([], []),
                                 {'value': None, 'n': 0, 'reason': 'insufficient_pairs'})
                self.assertEqual(function([1, 1, 1], [1, 2, 3]),
                                 {'value': None, 'n': 3, 'reason': 'constant_input'})

    def test_correlations_reject_unequal_length_instead_of_truncating(self):
        for function in (statistics.pearson, statistics.spearman, statistics.kendall_tau_b):
            with self.subTest(function=function.__name__), self.assertRaises(ValueError):
                function([1, 2], [1])

    def test_pearson_is_finite_at_extreme_scales(self):
        for values in ([1e308, 0., -1e308], [1e-300, 2e-300, 3e-300]):
            with self.subTest(values=values):
                result = statistics.pearson(values, [-value for value in values])
                self.assertTrue(math.isfinite(result['value']))
                self.assertAlmostEqual(result['value'], -1.)


if __name__ == '__main__':
    unittest.main()
