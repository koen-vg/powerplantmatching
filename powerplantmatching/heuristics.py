# -*- coding: utf-8 -*-
## Copyright 2015-2016 Fabian Hofmann (FIAS), Jonas Hoersch (FIAS)

## This program is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 3 of the
## License, or (at your option) any later version.

## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.

## You should have received a copy of the GNU General Public License
## along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
Functions to modify and adjust power plant datasets
"""

from __future__ import absolute_import, print_function
import pandas as pd
import numpy as np
from .utils import lookup, _data_in
from .config import fueltype_to_life
from .cleaning import (clean_single, clean_technology)
import logging
from six import iteritems
logger = logging.getLogger(__name__)


def extend_by_non_matched(df, extend_by, label=None, fueltypes=None,
                          clean_added_data=True, use_saved_aggregation=True):
    """
    Returns the matched dataframe with additional entries of non-matched powerplants
    of a reliable source.

    Parameters
    ----------
    df : Pandas.DataFrame
        Already matched dataset which should be extended
    extend_by : pd.DataFrame | str
        Database which is partially included in the matched dataset, but
        which should be included totally. If str is passed, is will be used
        to call the corresponding data from data.py
    label : str
        Column name of the additional database within the matched dataset, this
        string is used if the columns of the additional database do not correspond
        to the ones of the dataset
    """
    from .data import data_config

    if isinstance(extend_by, str):
        label = extend_by
        extend_by = data_config[label]['read_function']()

    included_ids = df.projectID.map(lambda d: d.get(label)).dropna().sum()
    remaining_ids = ~ extend_by.projectID.isin(included_ids)

    extend_by = extend_by.loc[remaining_ids]

    if fueltypes is not None:
        extend_by = extend_by[extend_by.Fueltype.isin(fueltypes)]
    if clean_added_data:
        extend_by = clean_single(extend_by, use_saved_aggregation=use_saved_aggregation,
                                 dataset_name=label)
    extend_by = extend_by.assign(projectID = extend_by.projectID.map(lambda x: {label : x}))
    return df.append(extend_by.reindex(columns=df.columns), ignore_index=True)


def rescale_capacities_to_country_totals(df, fueltypes):
    """
    Returns a extra column 'Scaled Capacity' with an up or down scaled capacity in
    order to match the statistics of the ENTSOe country totals. For every
    country the information about the total capacity of each fueltype is given.
    The scaling factor is determined by the ratio of the aggregated capacity of the
    fueltype within each coutry and the ENTSOe statistics about the fueltype capacity
    total within each country.

    Parameters
    ----------
    df : Pandas.DataFrame
        Data set that should be modified
    fueltype : str or list of strings
        fueltype that should be scaled
    """
    from .data import Capacity_stats
    df = df.copy()
    if isinstance(fueltypes, str):
        fueltypes = [fueltypes]
    stats_df = lookup(df).loc[fueltypes]
    stats_entsoe = lookup(Capacity_stats()).loc[fueltypes]
    if ((stats_df==0)&(stats_entsoe!=0)).any().any():
        print('Could not scale powerplants in the countries %s because of no occurring \
              power plants in these countries'%\
              stats_df.loc[:, ((stats_df==0)&\
                            (stats_entsoe!=0)).any()].columns.tolist())
    ratio = (stats_entsoe/stats_df).fillna(1)
    df.loc[:, 'Scaled Capacity'] = df.loc[:, 'Capacity']
    for country in ratio:
        for fueltype in fueltypes:
            df.loc[(df.Country==country)&(df.Fueltype==fueltype), 'Scaled Capacity'] *= \
                   ratio.loc[fueltype,country]
    return df

def fill_missing_duration(df):
    mean_duration = df[df.Set=='Store'].groupby('Fueltype').Duration.mean()
    for store in mean_duration.index:
        df.loc[(df['Set']=='Store') & (df['Fueltype']==store), 'Duration'] = \
                                                        mean_duration.at[store]
    return df

def extend_by_VRE(df, base_year, prune_beyond=True):
    """
    Extends a given reduced dataframe by externally given VREs.

    Parameters
    ----------
    df : pandas.DataFrame
        The dataframe to be extended
    base_year : int
        Needed for deriving cohorts from IRENA's capacity statistics

    Returns
    -------
    df : pd.DataFrame
         Extended dataframe
    """
    from .data import IRENA_stats, OPSD_VRE
    df = df.copy()
    # Drop Solar (except CSP), Wind and Bioenergy which are to be replaced
    df = df[~(((df.Fueltype=='Solar')&(df.Technology!='CSP'))|
              (df.Fueltype=='Wind')|(df.Fueltype=='Bioenergy'))]
    cols = df.columns
    # Take CH, DE, DK values from OPSD
    logger.info('Read OPSD_VRE dataframe...')
    vre_CH_DE_DK = OPSD_VRE().loc[lambda x: x.Fueltype.isin(['Solar', 'Wind', 'Bioenergy'])]
    vre_DK = vre_CH_DE_DK[vre_CH_DE_DK.Country=='Denmark']
    vre_CH_DE = vre_CH_DE_DK[vre_CH_DE_DK.Country!='Denmark']
    logger.info('Aggregate CH+DE by commyear')
    vre_CH_DE = aggregate_VRE_by_commyear(vre_CH_DE)
    vre_CH_DE.loc[:, 'File'] = 'renewable_power_plants.sqlite'
    # Take other countries from IRENA stats without: DE, DK_Wind+Solar+Hydro, CH_Bioenergy
    logger.info('Read IRENA_stats dataframe...')
    vre = IRENA_stats().loc[lambda x: x.Fueltype.isin(['Solar', 'Wind', 'Bioenergy'])]
    vre = vre[~(vre.Country=='Germany')]
    vre = vre[~((vre.Country=='Denmark')&((vre.Fueltype=='Wind')|(vre.Fueltype=='Solar')|(vre.Fueltype=='Hydro')))]
    vre = vre[~((vre.Country=='Switzerland')&(vre.Fueltype=='Bioenergy'))]
    vre = vre[~(vre.Technology=='CSP')] # Drop IRENA's CSP. Data seems to be outdated
    vre = derive_vintage_cohorts_from_statistics(vre, base_year=base_year)
    vre.loc[:, 'File'] ='IRENA_CapacityStatistics2017.csv'
    # Concatenate
    logger.info('Concatenate...')
    cc = pd.concat([df, vre_DK, vre_CH_DE, vre], ignore_index=True)
    cc = cc.loc[:,cols]
    if prune_beyond:
        cc = cc[(cc.YearCommissioned<=base_year)|cc.YearCommissioned.isnull()]
    cc.reset_index(drop=True, inplace=True)
    return cc


def average_empty_commyears(df):
    """
    Fills the empty commissioning years with averages.
    """
    df = df.copy()
    #mean_yrs = df.groupby(['Country', 'Fueltype']).YearCommissioned.mean().unstack(0)
    # 1st try: Fill with both country- and fueltypespecific averages
    df.YearCommissioned.fillna(df.groupby(['Country', 'Fueltype']).YearCommissioned
                               .transform("mean"), inplace=True)
    # 2nd try: Fill remaining with only fueltype-specific average
    df.YearCommissioned.fillna(df.groupby(['Fueltype']).YearCommissioned
                               .transform('mean'), inplace=True)
    # 3rd try: Fill remaining with only country-specific average
    df.YearCommissioned.fillna(df.groupby(['Country']).YearCommissioned
                               .transform('mean'), inplace=True)
    if df.YearCommissioned.isnull().any():
        count = len(df[df.YearCommissioned.isnull()])
        raise(ValueError('''There are still *{0}* empty values for 'YearCommissioned'
                            in the DataFrame. These should be either be filled
                            manually or dropped to continue.'''.format(count)))
    df.loc[:, 'YearCommissioned'] = df.YearCommissioned.astype(int)
    return df


def aggregate_VRE_by_commyear(df, target_fueltypes=None, agg_geo_by=None):
    """
    Aggregate the vast number of VRE (e.g. vom data.OPSD_VRE()) units to one
    specific (Fueltype + Technology) cohorte per commissioning year.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame containing the data to aggregate
    target_fueltypes : list
        list of fueltypes to be aggregated (Others are cutted!)
    agg_by_geo : str
        How to deal with lat/lon positions. Allowed:
            NoneType : Do not show geoposition at all
            'mean'   : Average geoposition
            'wm'     : Average geoposition weighted by capacity
    """
    df = df.copy()
    if agg_geo_by is None:
        f = {'Capacity':['sum']}
    elif agg_geo_by == 'mean':
        f = {'Capacity':['sum'], 'lat':['mean'], 'lon':['mean']}
    elif agg_geo_by == 'wm':
        #TODO: This does not work yet, when NaNs are in lat/lon columns.
        wm = lambda x: np.average(x, weights=df.loc[x.index, 'Capacity'])
        f = {'Capacity':['sum'], 'lat':{'weighted mean':wm}, 'lon':{'weighted mean':wm}}
    else:
        raise TypeError("Value given for `agg_geo_by` is '{}' but must be either \
                        'NoneType' or 'mean' or 'wm'.".format(agg_geo_by))

    if target_fueltypes is None:
        target_fueltypes = ['Wind', 'Solar', 'Bioenergy']
    df = df[df.Fueltype.isin(target_fueltypes)]
    df = average_empty_commyears(df)
    df.Technology.fillna('-', inplace=True)
    df = df.groupby(['Country','YearCommissioned','Fueltype','Technology'])\
           .agg(f).reset_index().replace({'-': np.NaN})
    df.columns = df.columns.droplevel(level=1)
    df.loc[:, 'Set'] = 'PP'
    return df


def derive_vintage_cohorts_from_statistics(df, base_year=2015):
    """
    This function assumes an age-distribution for given capacity statistics
    and returns a df, containing how much of capacity has been built for every
    year.
    """
    def setInitial_Flat(mat, df, life):
        y_start = df.index[0]
        height_flat = float(df.loc[y_start].Capacity) / life
        for y in range(int(mat.index[0]), y_start+1):
            y_end = min(y+life-1, mat.columns[-1])
            mat.loc[y, y:y_end] = height_flat
        return mat

    def setInitial_Triangle(mat, df, life):
        y_start = df.index[0]
        years = range(y_start-life+1, y_start+1)
        height_flat = float(df.loc[y_start].Capacity) / life
        decr = 2.0*height_flat/life              # decrement per period, 'slope' of the triangle
        height_tri = 2.0*height_flat - decr/2.0  # height of triangle at right side
        series = [(height_tri - i*decr) for i in range(0, life)][::-1]
        dic = dict(zip(years, series))           # create dictionary
        for y in range(int(mat.index[0]), y_start+1):
            y_end = min(y+life-1, mat.columns[-1])
            mat.loc[y, y:y_end] = dic[y]
        return mat

    def setHistorical(mat, df, life):
        year = df.index[1]  # Base year was already handled in setInitial()->Start one year later.
        while year <= df.index.max():
            if year in df.index:
                addition = df.loc[year].Capacity - mat.loc[:, year].sum()
                if addition >= 0:
                    mat.loc[year, year:year+life-1] = addition
                else:
                    mat.loc[year, year:year+life-1] = 0
                    mat = reduceVintages(addition, mat, life, year)
            else:
                mat.loc[year, year:year+life-1] = 0
            year += 1
        return mat

    def reduceVintages(addition, mat, life, y_pres):
        for year in mat.index:
            val_rem = float(mat.loc[year, y_pres])
            # print ('In year %i are %.2f units left from year %i, while addition '\
            #        'delta is %.2f'%(y_pres, val_rem, year, addition))
            if val_rem > 0:
                if abs(addition) > val_rem:
                    mat.loc[year, y_pres:year+life-1] = 0
                    addition += val_rem
                else:
                    mat.loc[year, y_pres:year+life-1] = val_rem + addition
                    break
        return mat

    dfe = pd.DataFrame(columns=df.columns)
    for c, df_country in df.groupby(['Country']):
        for tech, dfs in df_country.groupby(['Technology']):
            dfs.set_index('Year', drop=False, inplace=True)
            y_start = dfs.index[0]
            y_end = dfs.index[-1]
            life = fueltype_to_life()[dfs.Fueltype.iloc[0]]
            mat = pd.DataFrame(columns=range(y_start-life+1, y_end+life),
                               index=range(y_start-life+1, y_end)).astype(np.float)
            if dfs.Fueltype.iloc[0] in ['Solar', 'Wind', 'Bioenergy', 'Geothermal']:
                mat = setInitial_Triangle(mat, dfs, life)
            else:
                mat = setInitial_Flat(mat, dfs, life)
            if y_end > y_start:
                mat = setHistorical(mat, dfs, life)
            add = pd.DataFrame(columns=dfs.columns)
            add.Capacity = list(mat.loc[:, base_year])
            add.Year = mat.index.tolist()
            add.Technology = tech
            add.Country = c
            add.Fueltype = dfs.Fueltype.iloc[0]
            add.Set = dfs.Set.iloc[0]
            dfe = pd.concat([dfe, add[add.Capacity>0.0]], ignore_index=True)
    dfe.Year = dfe.Year.apply(pd.to_numeric)
    dfe.rename(columns={'Year':'YearCommissioned'}, inplace=True)
    return dfe[~np.isclose(dfe.Capacity, 0)]


def manual_corrections(df):
    """
    Here, manual corrections are being processed which are not (yet) solved by the
    data mending, matching or reducing algorithms.
    """
    # 1. German CAES plant Huntorf
    df.loc[df.Name.str.contains('huntorf', case=False).fillna(False), 'Technology'] = 'CAES'
    return df


def set_denmark_region_id(df):
    """
    Used to set the Region column to DKE/DKW (East/West) for electricity models,
    based on lat,lon-coordinates and a heuristic for unknowns.
    """
    if 'Region' not in df:
        pos = [i for i,x in enumerate(df.columns) if x == 'Country'][0]
        df.insert(pos+1, 'Region', np.nan)
    else:
        if ('DKE' in set(df.Region))|('DKW' in set(df.Region)):
            return df
        df.loc[(df.Country=='Denmark'), 'Region'] = np.nan
    #TODO: This does not work yet.
        #import geopandas as gpd
        #df = gpd.read_file('/tmp/ne_10m_admin_0_countries/')
        #df = df.query("ISO_A2 != '-99'").set_index('ISO_A2')
        #Point(9, 52).within(df.loc['DE', 'geometry'])
    # Workaround:
    df.loc[(df.Country=='Denmark')&(df.lon>=10.96), 'Region'] = 'DKE'
    df.loc[(df.Country=='Denmark')&(df.lon<10.96), 'Region'] = 'DKW'
    df.loc[df.Name.str.contains('Jegerspris', case=False).fillna(False), 'Region'] = 'DKE'
    df.loc[df.Name.str.contains('Jetsmark', case=False).fillna(False), 'Region'] = 'DKW'
    df.loc[df.Name.str.contains('Fellinggard', case=False).fillna(False), 'Region'] = 'DKW'
    # Copy the remaining ones without Region and handle in copy
    dk_o = df.loc[(df.Country=='Denmark')&(df.Region.isnull())].reset_index(drop=True)
    dk_o.loc[:, 'Capacity'] *= 0.5
    dk_o.loc[:, 'Region'] = 'DKE'
    # Handle remaining in df
    df.loc[(df.Country=='Denmark')&(df.Region.isnull()), 'Capacity'] *= 0.5
    df.loc[(df.Country=='Denmark')&(df.Region.isnull()), 'Region'] = 'DKW'
    # Concat
    df = pd.concat([df, dk_o], ignore_index=True)
    return df


def remove_oversea_areas(df, lat=[36, 72], lon=[-10.6, 31]):
    """
    Remove plants outside continental Europe such as the Canarian Islands etc.
    """
    df = df.loc[(df.lat.isnull() | df.lon.isnull()) |
                ((df.lat>=lat[0]) & (df.lat<=lat[1]) & (df.lon>=lon[0]) & (df.lon<=lon[1]))]
    return df


def gross_to_net_factors(reference='opsd', aggfunc='median', return_entire_data=False):
    """
    """
    if reference=='opsd':
        from .data import OPSD
        reference = OPSD(rawDE=True)
    df = reference.copy()
    df = df[df.capacity_gross_uba.notnull()&df.capacity_net_bnetza.notnull()]
    df.loc[:, 'ratio'] = df.capacity_net_bnetza / df.capacity_gross_uba
    df = df[df.ratio<=1.0] # these are obvious data errors
    if return_entire_data:
        return df
    else:
        df.energy_source_level_2.fillna(value=df.fuel, inplace=True)
        df.replace(dict(energy_source_level_2={'Biomass and biogas': 'Bioenergy',
                                    'Fossil fuels': 'Other',
                                    'Mixed fossil fuels': 'Other',
                                    'Natural gas': 'Natural Gas',
                                    'Non-renewable waste': 'Waste',
                                    'Other bioenergy and renewable waste': 'Bioenergy',
                                    'Other or unspecified energy sources': 'Other',
                                    'Other fossil fuels': 'Other',
                                    'Other fuels': 'Other'}), inplace=True)
        df.rename(columns={'technology':'Technology'}, inplace=True)
        df = (clean_technology(df)
                .assign(energy_source_level_2=lambda df: df.energy_source_level_2.str.title()))
        ratios = df.groupby(['energy_source_level_2', 'Technology']).ratio.mean()
        return ratios


def scale_to_net_capacities(df, is_gross=True, catch_all=True):
    if is_gross:
        factors = gross_to_net_factors()
        for ftype, tech in factors.index.get_values():
            df.loc[(df.Fueltype==ftype)&(df.Technology==tech), 'Capacity'] *= factors.loc[(ftype, tech)]
        if catch_all:
            for ftype in factors.index.levels[0]:
                techs = factors.loc[ftype].index.tolist()
                df.loc[(df.Fueltype==ftype)&(~df.Technology.isin(techs)), 'Capacity'] *= (
                        factors.loc[ftype].mean())
        return df
    else:
        return df


def PLZ_to_LatLon_map():
    return pd.read_csv(_data_in('PLZ_Coords_map.csv'), index_col='PLZ')


def set_known_retire_years(df):
    """
    Integrate known retire years, e.g. for German nuclear plants with fixed
    decommissioning dates.
    """

    if 'YearRetire' not in df:
        df['YearRetire'] = np.nan

    YearRetire = {
        'Grafenrheinfeld': 2015,
        'Philippsburg': 2019,
        'Brokdorf': 2021,
        'Grohnde': 2021,
        'Gundremmingen': 2021,
        'Emsland': 2022,
        'Isar': 2022,
        'Neckarwestheim': 2022
    }

    ppl_de_nuc = pd.DataFrame(df.loc[(df.Country == 'Germany') & (df.Fueltype == 'Nuclear'),
                                     ['Name', 'YearRetire']])
    for name, year in iteritems(YearRetire):
        name_match_b = ppl_de_nuc.Name.str.contains(name, case=False, na=False)
        if name_match_b.any():
            ppl_de_nuc.loc[name_match_b, 'YearRetire'] = year
        else:
            logger.warn("'{}' was not found in given DataFrame.".format(name))
    df.loc[ppl_de_nuc.index, 'YearRetire'] = ppl_de_nuc['YearRetire']
    return df
