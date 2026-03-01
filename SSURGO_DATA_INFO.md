SSURGO Data Packaging and Use November 2012 United States Department of
Agriculture Natural Resources Conservation Service

Table of Contents INTRODUCTION
........................................................................................................................................
5 SSURGO EXPORT PACKAGING
............................................................................................................
6 CONTENTS OF DIRECTORY "SOIL_SSASYMBOL"
.........................................................................................
6 CONTENTS OF DIRECTORY "TABULAR"
......................................................................................................
7 CONTENTS OF DIRECTORY "SPATIAL"
........................................................................................................
7 Non-Spatial Attributes Embedded in the Spatial Data
..........................................................................
9 DATA VERSIONING
................................................................................................................................
10 LOCATING VERSION INFORMATION
..........................................................................................................
10 MAKING SURE THE TABULAR DATAVERSION IS COMPATIBLE WITH THE SPATIAL
DATAVERSION ......... 11 USING SOIL TABULAR DATA
..............................................................................................................
12 USING A SSURGO T EMPLATE DATABASE
...............................................................................................
13 USING SOIL SPATIAL DATA
................................................................................................................
13 PERSONAL GEODATABASE
.......................................................................................................................
14 THECHALLENGE OF USING SOILDATA IN A GIS
.....................................................................................
15 Thematic Map Generation
..................................................................................................................
15 Methods of
Aggregation......................................................................................................................
15 Dominant Component
.....................................................................................................................................
17 Dominant
Condition........................................................................................................................................
18 Most Limiting
.................................................................................................................................................
18 Least
Limiting.................................................................................................................................................
19 Weighted
Average...........................................................................................................................................
19 All Components
..............................................................................................................................................
20
Presence/Absence............................................................................................................................................
21 Soil Data Viewer Application
.............................................................................................................
21 CONTACTING SUPPORT
.......................................................................................................................
22

SSURGO Data Packaging and Use Document Version: 6 Page: 5 of
22Introduction Soil survey data can be downloaded from the Soil Data
Mart: http://soildatamart.nrcs.usda.gov Data for a soil survey area
includes a tabular component and a spatial component. The tabular
component is typically imported into a database for querying, reporting
and analysis. The spatial component is typically viewed and analyzed
using a Geographic Information System (GIS). Although a finished soil
survey area always includes both components, a survey area's tabular
component may be available in the Soil Data Mart long before the
corresponding spatial component. A soil survey area's tabular component
can be downloaded independent of its corresponding spatial component,
and vice versa. A survey area cannot be posted to the Soil Data Mart
until its tabular component is available. Therefore there will never be
a soil survey area in the Soil Data Mart for which only the spatial
component is available. Data from the Soil Data Mart is distributed in
what is referred to as "SSURGO" format. For the tabular component, this
format dictates which soil attributes are included, how those attributes
are defined, how those attributes are grouped and how those groups are
related. For the spatial component, this format dictates which spatial
layers are defined, which spatial layers are mandatory and the standards
to which that spatial data conforms. The SSURGO format has evolved over
time. There have been two major versions and four minor versions. The
major differences between these versions are summarized below. SSURGO
Version Date Description 1.0 circa 1990? The format of the tabular
component is based on the State Soil Survey database (SSSD), the
precursor to NASIS (National Soil Information System). 2.0 January
2001The format of the tabular component is based on the NASIS database.
2.1 December 2003The format of the tabular and spatial component is
modified in order to support more explicit versioning of soil survey
data. This format became available with the advent of the Soil Data
Mart. 2.2 October 2005The format of the tabular component is modified to
include the data necessary to drive the Soil Data Viewer application.

SSURGO Data Packaging and Use Document Version: 6 Page: 6 of 22SSURGO
Export Packaging When soil data is exported from the Soil Data Mart, the
end result is always a single zip file, regardless of what export
options were selected. The format of an export file name is:
soil_ssasymbol.zip, wheressasymbolis the symbol of the corresponding
soil survey area. A soil survey area symbol uniquely identifies a soil
survey area. An export from the Soil Data Mart always contains data for
one and only one soil survey area. A survey area symbol is a five
character string where the first two characters are a U.S. state or
territory postal code, and the last three characters are digits that
represent the soil survey area within the corresponding state or
territory. A SSURGO export file can be unzipped using WinZip or an
equivalent application. When an export file is unzipped, the following
directory hierarchy is produced in the directory to which the export
file was unzipped: soil_ssasymbol tabular spatial wheressasymbolis the
symbol of the corresponding soil survey area. Contents of Directory
"soil_ssasymbol" In addition to subdirectories "tabular" and "spatial",
this directory contains either three or four files: 1.
soil_metadata_ssasymbol.txt 2. soil_metadata_ssasymbol.xml 3. readme.txt
4.something.zip (optional) The first two files contain the FGDC metadata
for the corresponding survey area, in plain ASCII and XML format,
respectively. FGDC is the acronym for Federal Geographic Data Committee.
The FGDC metadata primarily pertains to spatial soil data. A smaller
portion of the FGDC metadata pertains to the tabular soil data. File
"readme.txt" contains a lot of the same information that is presented in
this document. The top of this file documents the versions of the data
included in the export, as well as any options that were specified if
spatial data was included in the export.

SSURGO Data Packaging and Use Document Version: 6 Page: 7 of 22File
"something.zip", if it exists, is a zipped Microsoft Access database,
into which the tabular soil data can be imported. This file will only
exist if the person who generated this export requested its inclusion.
The embedded Microsoft Access database is referred to as a "SSURGO
template database". There is more than one SSURGO template database, so
the root portion of the file name can vary. SSURGO template databases
are discussed in the section titled "Using Soil Tabular Data". If a
SSURGO template database was not included in the export, one can always
be downloaded from the following Soil Data Mart web page:
http://soildatamart.nrcs.usda.gov/Templates.aspx Contents of Directory
"tabular" Directory "tabular" contains a set of ASCII field and text
delimited files. Fields are delimited by the pipe or vertical bar
character. Text fields are double quote delimited because such a field
can contain embedded field delimiters and/or line ends. Within a text
field, any embedded double quotes are doubled. With the exception of
"version.txt", each of these files corresponds to a table in a SSURGO
template database. Which ASCII delimited file ("Import/Export File
Name") corresponds to which database table ("Table Physical Name") is
documented in the "SSURGO Metadata -- Tables" report. This report is
available at http://soildatamart.nrcs.usda.gov/ssurgometadata.aspx .
File "version.txt" records the SSURGO version of the corresponding
tabular data. The content of this file is checked when tabular data is
imported into a SSURGO template database. Contents of Directory
"spatial" The exact content of directory "spatial" depends on the
spatial format option that was selected when the export was generated.
Spatial data can be exported in any of the following formats: 1. ESRI
Shape File 2. ArcInfo Coverage 3. ArcInfo Interchange In addition to
specifying a spatial format, a user exporting soil spatial data from the
Soil Data Mart can specify a coordinate system and projection. When
spatial data is included in an export, the corresponding spatial format
and coordinate system are documented in the "readme.txt" file, located
in the root directory that was created by unzipping the export file. The
export spatial

SSURGO Data Packaging and Use Document Version: 6 Page: 8 of 22format
and coordinate system are also documented in the FGDC metadata, also
located in the root directory that was created by unzipping the export
file. In discussing the spatial data, it helps to separate the logical
from the physical. The possible logical spatial entities are: 1. Soil
Survey Area Boundary Polygon(s) (Required) 2. Map Unit Boundary Polygons
(Required) 3. Line Map Units (Optional) 4. Point Map Units (Optional) 5.
Line Spot Features (Optional) 6. Point Spot Features (Optional) 7. Spot
Feature Descriptions (Required if Line Spot Features or Point Spot
Features are included) Each of the first six spatial entities represent
what is referred to as a "feature class". The last entity, Spot Feature
Descriptions, is an ASCII field and text delimited file that contains
narrative descriptions of any corresponding line or point spot features.
In effect, this is "tabular data" that is considered to be part of the
spatial data. "Required" means that, if spatial data for the
corresponding survey area exists, this data must be included. In other
words, if spatial data exists, at a minimum, a soil survey area boundary
polygon feature class and a map unit boundary polygon feature class will
exist. A single logical spatial entity is delivered as multiple files,
sometimes with one or more subdirectories, depending on the spatial
format that was selected when the export was generated. Some, but not
necessarily all, of the files corresponding to a particular logical
spatial entity have a name whose prefix denotes the corresponding
spatial entity. For spatial data in ESRI Shape File format, the
following file name prefixes are used: File Name Prefix Spatial Entity
soilsa_a soil survey area boundary polygon(s) soilmu_a map unit boundary
polygons soilmu_l line map units soilmu_p point map units soilsf_l line
spot features soilsf_p point spot features soilsf_t spot feature
descriptions

SSURGO Data Packaging and Use Document Version: 6 Page: 9 of 22For
spatial data in ArcInfo Coverage or ArcInfo Interchange format, the
following file name prefixes are used: File Name Prefix Spatial Entity
ssa_a soil survey area boundary polygon(s) smu_a map unit boundary
polygons smu_l line map units smu_p point map units ssf_l line spot
features ssf_p point spot features ssf_t spot feature descriptions
Non-Spatial Attributes Embedded in the Spatial Data A handful of
non-spatial (non-geometry) attributes are embedded into the various
spatial feature classes. These attributes either: (1) logically identify
a corresponding tabular entity, (2) serve as the physical link to the
corresponding tabular entity, or (3) identify the corresponding spatial
data version. See the section titled "Data Versioning" for more
information about data versioning. Attribute Present in the Following
Spatial Feature Classes or Files Purpose Survey Area Symbol (column name
is always "areasymbol")All Logically identifies the corresponding survey
area. Spatial Data Version (column name is either "spatialversion" or
"spatialver" -- for some spatial formats, column names are limited to no
more than ten characters)All Logically identifies the corresponding
spatial data version. Legend Key (column name is always "lkey")Survey
Area Boundary Polygon(s)Links survey area boundary spatial record to the
corresponding legend table record. Map Unit Symbol (column name is
always "musym")Map Unit Boundary Polygons, Line Map Units, Point Map
UnitsLogically identifies the corresponding map unit. Map Unit Key
(column name is always "mukey")Map Unit Boundary Polygons, Line Map
Units, Point Map UnitsLinks map unit boundary spatial record to the
corresponding mapunit table record. Spot Feature Symbol Line Spot
Features, Point Logically identifies the

SSURGO Data Packaging and Use Document Version: 6 Page: 10 of 22(column
name is always "featsym")Spot Features, Spot Feature Descriptions
Filecorresponding spot feature. Spot Feature Key (column name is always
"featkey")Line Spot Features, Point Spot Features, Spot Feature
Descriptions FileLinks spot feature spatial record to corresponding
feature description record. Data Versioning With the advent of the Soil
Data Mart, data for a soil survey area is now explicitly versioned.
There are three different versions: 1. Survey Area Version 2. Tabular
Data Version 3. Spatial Data Version Since tabular soil data for a
survey area can be updated without updating the corresponding spatial
soil data, and vice versa, tabular data and spatial data are versioned
independently of one another. For a survey area, any new Tabular Data
Version or Spatial Data Version results in a new Survey Area Version.
For any version, there are two version attributes: 1. Version Number
(usually referred to as "version") 2. Version Established Date and Time
Version numbers are serially incremented integer values, starting at
one. Locating Version Information Survey area version information can be
found in the following locations: 1. In the readme.txt file, located in
the root directory that was created by unzipping the export file. 2. If
tabular data is included in the export, in the sacatalog table record
for the corresponding survey area (file sacatlog.txt or table sacatalog
when that tabular data has been imported into a SSURGO template
database).

SSURGO Data Packaging and Use Document Version: 6 Page: 11 of 223. In
the page footer of soil reports in the MS Access SSURGO template
database, when tabular data is included in the export and that tabular
data has been imported into a SSURGO template database. 4. In soil
report "Survey Area Data Summary" in the MS Access SSURGO template
database, when tabular data is included in the export and that tabular
data has been imported into a SSURGO template database. Tabular data
version information can be found in the following locations: 1. In the
readme.txt file, located in the root directory that was created by
unzipping the export file, when tabular data is included in the export.
2. If tabular data is included in the export, in the sacatalog table
record for the corresponding survey area (file sacatlog.txt or table
sacatalog when that tabular data has been imported into a SSURGO
template database). 3. In the body of soil report "Survey Area Data
Summary" in the MS Access SSURGO template database, when tabular data is
included in the export and that tabular data has been imported into a
SSURGO template database. Spatial data version information can be found
in the following locations: 1. In the readme.txt file, located in the
root directory that was created by unzipping the export file, when
spatial data is included in the export. 2. Embedded in spatial data for
any spatial feature class, when spatial data is included in the export.
Making Sure the Tabular Data Version is compatible with the Spatial Data
Version Soil tabular and spatial data are compatible when: 1. For every
survey area polygon feature, there is a corresponding legend table
record. All survey area features with the same area symbol share the
same legend table record. 2. For every map unit polygon, line or point
feature, there is a corresponding mapunit table record. All map unit
features with the same map unit symbol share the same mapunit table
record. Every mapunit table record IS NOT required to have a
corresponding map unit feature. While this is rare, it is permitted. The
case for which this exception is permitted is for a survey area in the
process of being mapped, where the spatial data is not yet complete.

SSURGO Data Packaging and Use Document Version: 6 Page: 12 of 22If the
export file includes both tabular and spatial data, that tabular data is
compatible with that spatial data. Where things can get out of sync is
when tabular data is obtained independently of its corresponding spatial
data, or vice versa. On your PC there is no easy way to verify if a set
of tabular and spatial data are compatible. The most reliable way to
verify compatibility is to visit the Soil Data Mart, access the Download
page for the survey area in question, and look at the tabular and
spatial version numbers displayed on the Download page. If your version
numbers don't match the version numbers displayed on the Download page,
you should request a new export that includes both tabular and spatial
data. Using Soil Tabular Data The tabular data as delivered in a SSURGO
export file isn't very usable as is. The tabular data is distributed
between approximately sixty ASCII delimited files. In order to
effectively use this data, it needs to be imported into a database. Even
after loading data into a database, you still need to understand what
the tables and attributes represent, how tables are related and what
data constraints are in place. This type of information is available on
the following web page:
http://soildatamart.nrcs.usda.gov/ssurgometadata.aspx The above URL is
also the location of this document. At the current time we provide a
Microsoft Access database into which soil tabular data can be imported.
We refer to this database as a "SSURGO template database". In order to
use this database, you have to have Microsoft Access installed on your
PC. In a SSURGO template database, the SSURGO database structure has
already been created. Tabular soil data can be imported by running a
macro that resides in the database. Once data has been imported, a
variety of reports can be generated. If you are willing to learn a
little bit about Microsoft Access, you can create your own queries
against the data you have imported. If a SSURGO template database was
included with the exported data, it will be the only zipped file in the
root directory that was created by unzipping the export. If a SSURGO
template database was not included with the exported data, one can be
downloaded from the following location:
http://soildatamart.nrcs.usda.gov/templates.aspx A number of different
SSURGO template databases are typically available. A national SSURGO
template database serves as the default. For a national SSURGO template
database, the corresponding "state code" in the template database web
page data grid is "US".

SSURGO Data Packaging and Use Document Version: 6 Page: 13 of 22Some
states have created a customized SSURGO template database for their
state. If a customized SSURGO template database is available for the
state in whose data you are interested, you should use that state's
customized SSURGO template database in lieu of the national SSURGO
template database. When selecting a SSURGO template database, if
possible, select one whose MS Access version is the same as the version
of MS Access that you have installed on your PC. At the current time,
national SSURGO template databases are available for Access 97, Access
2000 and Access 2002/2003. What MS Access versions are supported for a
state customized SSURGO template database varies from state to state.
While you can convert a SSURGO template database from one version of MS
Access to another, this conversion is not always successful. At the
current time, we do not support the use of any database other than MS
Access. If you are interested in loading soil tabular data into a
database other than MS Access, and have questions, please contact the
NASIS Hotline. See the section titled "Contacting Support" for the
details. Using a SSURGO Template Database For information about
importing tabular data into a template database, or information about
the capabilities of the SSURGO template database in general, do the
following: 1. Open the MS Access SSURGO template database in the
appropriate version of MS Access. 2. Click the Reports tab of the
Database Window. The Database Window may be behind a form titled "SSURGO
Import" or "Soil Reports". 3. Either double-click the report titled "How
to Understand and Use this Database", or select this report and then
click "Preview". 4. After the Report Viewer window is displayed, either
click the printer icon or select "Print" from the File menu. You can
also browse this report using the Preview window. Using Soil Spatial
Data The spatial data can't be used until it has been imported into a
GIS. In order to import soil spatial data into a GIS, the GIS must be
able to import one of the following spatial formats: 1. ESRI Shape File
2. ArcInfo Coverage 3. ArcInfo Interchange

SSURGO Data Packaging and Use Document Version: 6 Page: 14 of
22Obviously the spatial data isn't of much use unless the corresponding
tabular data is also available. Once you have successfully imported the
soil tabular data into a database, and have also successfully imported
soil spatial data into a GIS, it's pretty much uphill from there. See
the subsection titled "The Challenge of Using Soil Data in a GIS" for
more details. Personal Geodatabase If spatial data is available for the
survey in which you are interested, and if you have access to ArcGIS, a
personal geodatabase can be created. To do this, import the tabular data
into a SSURGO template database in the usual way, and then use
ArcCatalog to import each of the spatial data feature classes into that
same SSURGO template database. The spatial data to be imported must be
in either Shape File or Coverage format. The resulting personal
geodatabase can be used with ArcGIS or ArcView 8 or later. The personal
geodatabase doesn't know what tables are related unless you explicitly
create relationship classes to indicate how two tables are related. If
you want to establish relationship classes to any of the tabular data
tables, you must register ("register to the personal geodatabase") each
of the tabular tables of concern. The relationships between all SSURGO
tables are documented in the SSURGO data model diagrams, which can be
found at: http://soildatamart.nrcs.usda.gov/ssurgometadata.aspx If you
create a personal geodatabase and want to be able to link to spot
feature descriptions, the spot feature descriptions must be explicitly
imported into the SSURGO template database, but keep in mind that a
survey area may or may not include spot features. Spot feature
descriptions, when they exist, are bundled and versioned with the
spatial data, but the spot featuredescriptions are not a feature class.
To import spot feature descriptions into a SSURGO template database, run
the macro titled "Import Feature Descriptions". This macro will import
spot feature descriptions into the table named "featdesc". The macro
displays a dialog box that prompts for the fully qualified path of the
file containing the spot feature descriptions. This file, if it exists,
resides in the subdirectory named "spatial", and the name of the file
containing the spot feature descriptions starts with "soilsf_t" or
"ssf_t" for Shape File format or Coverage format, respectively. This
file has a ".txt" extension. One last thing to keep in mind is the size
limit of an MS Access database, which is 1 gigabyte for Access 97, and 2
gigabytes for Access 2000 and later versions. The amount of space
required for a given survey area's tabular and spatial data varies
widely. You may or may not be able to create a personal geodatabase that
includes data for more than one survey area, depending on the size of
the survey areas involved. For a single very large survey area, you may
be able to create a personal geodatabase, but performance may be
degraded.

SSURGO Data Packaging and Use Document Version: 6 Page: 15 of 22The
Challenge of Using Soil Data in a GIS For compatible tabular and spatial
data, the relationship between the tabular and spatial data has already
been established before that data was ever exported from the Soil Data
Mart. You are not responsible for establishing this relationship. (For
information about tabular and spatial data compatibility, see the
section titled "Making Sure the Tabular Data Version is compatible with
the Spatial Data Version". For information about how the tabular and
spatial data are linked, see the section titled "Non-Spatial Attributes
Embedded in the Spatial Data".) The challenge of using soil data in a
GIS is due to the following: 1. Thematic maps must be based on map
units. 2. The vast majority of attributes used to create a thematic map
are not attributes of a map unit but attributes of an entity that
repeats for its corresponding map unit. Thematic Map Generation Thematic
maps are a representation of reality and can only display a small part
of that reality in a single map. Information on the map is typically for
a single theme. Polygons in the digital map data are map unit
delineations. Therefore, themes for thematic maps are based on map
units. Many map polygons can be labeled the same, but all point to the
same record in the map unit table. Map units are typically made up of
one or more named soils. Other miscellaneous land types or areas of
water may be included. These entities and their percent compositions
make up the map unit components and define the map unit composition.
Soil components are typically composed of multiple horizons (layers).
Component attributes must be aggregated to a map unit level for map
visualization. Horizon attributes must be aggregated to the component
level, before components are aggregated to the map unit level. Horizon
attributes may be aggregated for the entire soil profile or for a
specific depth range. One may only be interested in the value of a
horizon attribute for the surface layer. Methods of Aggregation There
are a number of options for aggregating data to the map unit level. The
various options will be illustrated using selected columns from the
"component" and "chorizon" tables. These tables and columns are
described in reports "SSURGO Metadata - Tables" and "SSURGO Metadata -
Table Column Descriptions". These reports are available at
http://soildatamart.nrcs.usda.gov/ssurgometadata.aspx .

SSURGO Data Packaging and Use Document Version: 6 Page: 16 of 22In
SSURGO data, the field "mukey" uniquely identifies a map unit, and the
field "cokey" uniquely identifies a map unit component. Table: component
mukey (Map Unit Key) cokey (Component Key) comppct_r (Representative
Percent Composition) corcon (Corrosion Concrete) hydricrating (Hydric
Rating) 100017 100017:149541 60 low yes 100017 100017:149542 40 high yes
100049 100049:149613 50 low yes 100049 100049:149614 50 moderate no
147626 147626:215195 35 moderate no 147626 147626:215196 25 low no
147626 147626:215197 20 low no

SSURGO Data Packaging and Use Document Version: 6 Page: 17 of 22Table:
chorizon cokey (Component Key) hzdept_r (Representative Horizon Depth to
Top) hzdepb_r (Representative Horizon Depth to Bottom) claytotal_r
(Representative Clay Total Separate) 100017:149541 0 20 9.5
100017:149541 20 76 12.5 100017:149541 76 152 1.5 100017:149542 0 20 9.5
100017:149542 20 30 11.5 100017:149542 30 152 3.5 100049:149613 0 76 6.0
100049:149613 76 152 6.0 100049:149614 0 36 6.5 100049:149614 36 152 4.5
147626:215195 0 10 12.0 147626:215195 10 41 7.0 147626:215195 41 51
147626:215196 0 3 33.5 147626:215196 3 13 36.0 147626:215196 13 41 42.5
147626:215196 41 152 147626:215197 0 8 22.5 147626:215197 8 41 22.5
147626:215197 41 46 26.5 147626:215197 46 152 Dominant Component The
interpretation rating class or soil property value of the component with
the largest percent composition is used to class the map unit. If there
is more than one component that shares the highest percent composition,
a "tie-break rule" indicates which value should be selected. For this
example, in the case of a tie on percent composition, we'll choose the
more restrictive value. The results for Dominant Component for Corrosion
Concrete are shown below.

SSURGO Data Packaging and Use Document Version: 6 Page: 18 of 22mukey
comppct_r corcon 100017 60 low 100049 50 moderate (more restrictive
value was returned for the tie) 147626 35 moderate Dominant Condition
For the components in each map unit, like interpretation rating classes
or soil property values are grouped, and their corresponding percent
compositions are summed. The interpretation rating class or soil
property value for the group with the largest percent composition is
used to class the map unit. If there is more than one group that shares
the highest percent composition, a "tie- break rule" indicates which
value should be selected. For this example, in the case of a tie on
percent composition, we'll choose the more restrictive value. The
results for Dominant Condition for Corrosion Concrete are shown below.
mukey comppct_r corcon 100017 60 low 100049 50 moderate (more
restrictive value was returned for the tie) 147626 45 (25/low + 20/low)
low Most Limiting The most limiting interpretation rating class for all
of the components in a map unit is used to class the map unit. This
aggregation method is limited to interpretive type attributes where
which end of the spectrum is considered most limiting is already
established. The results for Most Limiting for Corrosion Concrete are
shown below.

SSURGO Data Packaging and Use Document Version: 6 Page: 19 of 22mukey
corcon 100017 high 100049 moderate 147626 moderate Least Limiting The
least limiting interpretation rating class for all of the components in
a map unit is used to class the map unit. This aggregation method is
limited to interpretive type attributes where which end of the spectrum
is considered least limiting is already established. The results for
Least Limiting for Corrosion Concrete are shown below. mukey corcon
100017 low 100049 low 147626 low Weighted Average A weighted average of
the soil property value for all components in the map unit is used to
class the map unit. Percent composition is used as the weighting factor.
Obviously this aggregation method is only suitable for numeric
attributes. If the soil property being aggregated is an attribute of a
soil horizon, for each component, a single value for all included
horizons must be determined before computing a weighted average for all
components. In this case, the single component value is also computed as
a weighted average, where the weighting factor for each horizon is its
percent of the depth range in question. For this example, let's derive a
weighted average value for total clay in the upper 50 centimeters of the
soil. Step 1 -- For each component, compute a weighted average value for
total clay in the upper 50 centimeters of the soil. Where soil depth is
less than 50 cm, use whatever lowest depth is available. Total Clay in
the Upper 50 cm (or less) =∑(horizon thickness/total thickness\*total
clay) for all included soil horizons

SSURGO Data Packaging and Use Document Version: 6 Page: 20 of 22cokey
comppct_r Total Clay in the Upper 50 cm (or less) 100017:149541 60
20/50*9.5 + 30/50*12.5 = 11.3 100017:149542 40 20/50*9.5 + 10/50*11.5 +
20/50*3.5 = 7.5 100049:149613 50 50/50*6.0 = 6.0 100049:149614 50
36/50*6.5 + 14/50*4.5 = 5.94 147626:215195 35 10/41*12.0 + 31/41*7.0 =
8.22 (no clay data was available below 41 cm) 147626:215196 25
3/41*33.5 + 10/41*36.0 + 28/41*42.5 = 40.26 (no clay data was available
below 41 cm) 147626:215197 20 8/46*22.5 + 33/46*22.5 + 5/46*26.5 = 22.93
(no clay data was available below 46 cm) Step 2 -- For each map unit,
compute a weighted average value for total clay in the upper 50
centimeters (or less) of the soil. Total Clay in the Upper 50 cm (or
less) =∑(percent composition\* Total Clay in the Upper 50 cm (or less))
for all components in the map unit mukey Total Clay in the Upper 50 cm
(or less) 100017 (60/100*11.3) + (40/100*7.5) = 9.78 100049
(50/100*6.0) + (50/100*5.94) = 5.97 147626 (35/80*8.22) +
(25/80*40.26) + (20/80\*22.93) = 21.91 All Components The highest or
lowest soil property value for all of the components in a map unit is
used to class the map unit. The results for All Components for Highest
Total Clay in the Surface Layer are shown below. mukey claytotal_r
100017 9.5 100049 12.0 147626 33.5

SSURGO Data Packaging and Use Document Version: 6 Page: 21 of
22Presence/Absence All of the components in a map unit are evaluated as
to whether a certain condition is present or absent, and the map unit is
then assigned one of the following four classes: 1. Presence or absence
of condition is always known, and condition is present for all
components. 2. Presence or absence of condition is always known, and
condition is never present for any component. 3. Presence or absence of
condition is not always known, but condition is present for some
components. 4. Presence or absence of condition is not always known, but
condition was not present for any component for which the condition was
known. The result classes may be assigned names more specific to the
condition in question. The results for Presence/Absence of Hydric
Components are shown below. mukey Hydric Components 100017 All Hydric
100049 Partially Hydric 147626 All Not Hydric Soil Data Viewer
Application The examples above represent relatively simple cases.
Dealing with missing data or missing percent composition hasn't been
addressed. None of the examples deal with attributes that don't have a
one to one relationship with either a component or soil horizon. To be
able to dynamically perform aggregation for large volumes of data, for a
variety of attributes, in anything resembling real time, requires
considerable programming. The functionality provided by commercial
applications like Microsoft Access or ESRI's GIS products don't help
much when it comes to aggregation. The NRCS solution to this problem was
to create an application that does the most commonly needed aggregations
on demand. This application is known as "Soil Data Viewer". Here is the
description of the Soil Data Viewer application from the Soil Data
Viewer User Guide:

SSURGO Data Packaging and Use Document Version: 6 Page: 22 of 22Soil
Data Viewer is a tool built as an extension to ArcMap that allows a user
to create soil-based thematic maps. The application can also be run
independent of ArcMap, but output is then limited to a tabular report.
The soil survey attribute database associated with the spatial soil map
is a complicated database with more than 50 tables. Soil Data Viewer
provides users access to soil interpretations and soil properties while
shielding them from the complexity of the soil database. Each soil map
unit, typically a set of polygons, may contain multiple soil components
that have different use and management. Soil Data Viewer makes it easy
to compute a single value for a map unit and display results, relieving
the user from the burden of querying the database, processing the data
and linking to the spatial map. Soil Data Viewer contains processing
rules to enforce appropriate use of the data. This provides the user
with a tool for quick geospatial analysis of soil data for use in
resource assessment and management. To see which version of Soil Data
Viewer is compatible with which version of ArcGIS (ArcMap is a component
of ArcGIS), please visit:
http://soildataviewer.nrcs.usda.gov/download.aspx Additional software
requirements are listed on the download page that corresponds to a
particular version of Soil Data Viewer. For additional information about
the capabilities of a particular version of Soil Data Viewer, please
visit: http://soildataviewer.nrcs.usda.gov/userguide.aspx Contacting
Support Questions should be directed to the National Soil Information
System (NASIS) Hotline. The NASIS Hotline, which resides at the USDA
NRCS National Soil Survey Center in Lincoln Nebraska, is staffed from
8:00 AM to 4:30 PM Central Time. (402) 437-5378 -- Steve Speidel (402)
437-5379 -- Tammy Cheever e-mail: hotline@lin.usda.gov
