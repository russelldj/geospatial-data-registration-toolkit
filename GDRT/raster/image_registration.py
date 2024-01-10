import shutil
import typing
import os
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import rasterio as rio
from matplotlib import pyplot as plt

from GDRT.constants import PATH_TYPE
from GDRT.geospatial_utils import get_projected_CRS
from GDRT.raster.utils import load_geospatial_crop


def cv2_feature_matcher(
    img1, img2, min_match_count=10, vis_matches=True, ransac_threshold=4.0
):
    # https://docs.opencv.org/3.4/d1/de0/tutorial_py_feature_homography.html
    # Initiate SIFT detector
    sift = cv2.SIFT_create()
    # find the keypoints and descriptors with SIFT
    kp1, des1 = sift.detectAndCompute(img1, None)
    kp2, des2 = sift.detectAndCompute(img2, None)
    FLANN_INDEX_KDTREE = 1
    index_params = dict(algorithm=FLANN_INDEX_KDTREE, trees=5)
    search_params = dict(checks=50)
    flann = cv2.FlannBasedMatcher(index_params, search_params)
    matches = flann.knnMatch(des1, des2, k=2)
    # store all the good matches as per Lowe's ratio test.
    good = []
    for m, n in matches:
        if m.distance < 0.7 * n.distance:
            good.append(m)

    if len(good) > min_match_count:
        src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        M, mask = cv2.estimateAffinePartial2D(
            src_pts,
            dst_pts,
            method=cv2.RANSAC,
            ransacReprojThreshold=ransac_threshold,
            maxIters=10000,
        )
        # Make a 3x3 matrix
        M = np.concatenate((M, np.array([[0, 0, 1]])))

    else:
        print("Not enough matches are found - {}/{}".format(len(good), min_match_count))
        return None

    if vis_matches:
        matchesMask = mask.astype(np.int32).ravel().tolist()
        h, w = img1.shape
        pts = np.float32([[0, 0], [0, h - 1], [w - 1, h - 1], [w - 1, 0]]).reshape(
            -1, 1, 2
        )
        dst = cv2.perspectiveTransform(pts, M)
        img2 = cv2.polylines(img2, [np.int32(dst)], True, 255, 3, cv2.LINE_AA)

        draw_params = dict(
            matchColor=(0, 255, 0),  # draw matches in green color
            singlePointColor=None,
            matchesMask=matchesMask,  # draw only inliers
            flags=2,
        )
        img3 = cv2.drawMatches(img1, kp1, img2, kp2, good, None, **draw_params)
        plt.imshow(img3, "gray"), plt.show()

    return M


def align_two_rasters(
    fixed_filename: PATH_TYPE,
    moving_filename: PATH_TYPE,
    output_filename: PATH_TYPE = None,
    region_of_interest: gpd.GeoDataFrame = None,
    target_GSD: typing.Union[None, float] = None,
    aligner_alg=cv2_feature_matcher,
    aligner_kwargs: dict = {},
    grayscale: bool = True,
    vis_chips: bool = True,
):
    """_summary_

    Args:
        fixed_filename (PATH_TYPE): _description_
        moving_filename (PATH_TYPE): _description_
        region_of_interest (gpd.GeoDataFrame, optional): _description_. Defaults to None.
        target_GSD (typing.Union[None, float], optional): _description_. Defaults to None.
        grayscale (bool, optional): _description_. Defaults to True.
        vis_chips (bool, optional): _description_. Defaults to True.

    Returns:
        _type_: _description_
    """
    # Use the fixed dataset to determine what CRS to use
    with rio.open(fixed_filename) as fixed_dataset:
        # Reproject both datasets into the same projected CRS
        if fixed_dataset.crs.is_projected:
            working_CRS = fixed_dataset.crs
        else:
            working_CRS = get_projected_CRS(
                lat=fixed_dataset.transform.c, lon=fixed_dataset.transform.f
            )

    # Extract an image chip from each input image, corresponding to the region of interest
    # TODO make sure that a None ROI loads the whole image
    fixed_chip, _, _ = load_geospatial_crop(
        fixed_filename,
        region_of_interest=region_of_interest,
        target_CRS=working_CRS,
        target_GSD=target_GSD,
    )

    (
        moving_chip,
        moving_window_transform,
        moving_dataset_transform,
    ) = load_geospatial_crop(
        moving_filename,
        region_of_interest=region_of_interest,
        target_CRS=working_CRS,
        target_GSD=target_GSD,
    )

    if grayscale:
        fixed_chip = cv2.cvtColor(fixed_chip, cv2.COLOR_BGR2GRAY)
        moving_chip = cv2.cvtColor(moving_chip, cv2.COLOR_BGR2GRAY)

    if vis_chips:
        _, ax = plt.subplots(1, 2)
        # TODO make these bounds more robust
        ax[0].imshow(fixed_chip, cmap="gray", vmin=0, vmax=255)
        ax[1].imshow(moving_chip, cmap="gray", vmin=0, vmax=255)
        ax[0].set_title("Fixed")
        ax[1].set_title("Moving")
        plt.show()

    # This is the potentially expensive step where we actually estimate a transform
    chip2chip_pixel_transform = aligner_alg(fixed_chip, moving_chip, **aligner_kwargs)

    # Matrix math to get useful quantities
    # Compute the transfrom mapping a pixel ID in the window to a pixel ID in the source
    w2d_px_transform = np.linalg.inv(moving_dataset_transform) @ moving_window_transform

    # TODO check if the center thing should be inverted
    dataset_pixel_transform = (
        w2d_px_transform @ chip2chip_pixel_transform @ np.linalg.inv(w2d_px_transform)
    )

    updated_moving_dataset_transform = (
        moving_dataset_transform @ dataset_pixel_transform
    )

    if output_filename is not None:
        output_filename = Path(output_filename)
        output_filename.parent.mkdir(exist_ok=True, parents=True)
        if not os.path.isfile(output_filename):
            shutil.copy(moving_filename, output_filename)

        # TODO the CRS should be examined
        with rio.open(output_filename, "r+") as dataset:
            dataset.transform = rio.guard_transform(updated_moving_dataset_transform[:2].flatten())
    return updated_moving_dataset_transform